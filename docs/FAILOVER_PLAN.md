# Kesha Failover v8: Codex-Approved

## Problem

Kesha on laptop. Unstable network → Claude API unreachable → empty responses.
Need automatic failover to VPS.

## Requirements

1. Both nodes independent
2. NEVER two pollers (hard safety)
3. Automatic failover + handback
4. Session continuity
5. Cannot prove lease ownership → suppress ALL side effects

## Safety Rule

**Cannot prove lease → suppress everything: polling, sends, drafts, reactions, reminders.**

Enforced at two levels:
- **Lease level**: `is_owner_now` = local deadline from last renew
- **Transport level**: ALL outgoing Telegram API calls gated

## Local Lease Deadline

```python
RENEW_INTERVAL = 30   # seconds
LEASE_TTL = 120       # Redis key TTL
LOCAL_TTL = 45        # local deadline (one missed renew + jitter)

@property
def is_owner_now(self) -> bool:
    return self._is_owner and time.time() < self._lease_valid_until
```

After 15s without successful renew → `is_owner_now` = False → all suppressed.

## Transport Fencing — aiogram Middleware (NOT FencedBot wrapper)

### Why Not FencedBot

Codex v6 P0: `FencedBot.__getattr__` misses `send_photo/video/voice/reaction`.
And `message.answer()` goes through original bot, bypassing wrapper entirely.

### Solution: aiogram OuterMiddleware on Bot.session

aiogram routes ALL API calls through `bot.session`. One middleware catches everything:
`send_message`, `send_photo`, `edit_message_text`, `set_message_reaction`, `message.answer()`, drafts — ALL of them.

```python
from aiogram.client.session.base import BaseSession
from aiogram.methods.base import TelegramMethod

class LeaseGateMiddleware:
    """Blocks ALL outgoing Telegram API calls when not lease owner."""

    def __init__(self, lease_manager):
        self._lm = lease_manager

    async def __call__(self, make_request, bot, method: TelegramMethod):
        if self._lm and not self._lm.is_owner_now:
            # Block EVERYTHING including GetUpdates — polling controlled via task cancel
            logger.debug(f"Suppressed {type(method).__name__} (not lease owner)")
            return None
        return await make_request(bot, method)

# Registration:
bot.session.middleware(LeaseGateMiddleware(lease_manager))
```

This catches **every** Telegram API call from any code path:
- `bot.send_message()` ✅
- `bot.send_photo()` ✅
- `bot.edit_message_text()` ✅
- `bot.set_message_reaction()` ✅
- `bot.send_chat_action()` ✅
- `message.answer()` ✅ (internally calls `bot.send_message`)
- `bot(SendMessageDraft(...))` ✅
- MCP tools `_bot_ref.bot.send_voice()` ✅
- ToolStatus `bot.edit_message_text()` ✅
- Compact `bot.send_message()` ✅

Zero code changes in existing handlers/tools. One middleware, total coverage.

**GetUpdates also blocked**: when not owner, ALL API calls suppressed including
polling. Polling task will fail/exit → detected by owner tick → fail_closed.

## LeaseManager — Complete

```python
class LeaseManager:
    RENEW_INTERVAL = 30
    LEASE_TTL = 120
    LOCAL_TTL = 45
    DRAIN_DEADLINE = 60
    HEALTH_RELEASE_STRIKES = 6  # 6 × 30s = 180s

    def __init__(self, node_id: str, redis_url: str):
        self.node_id = node_id
        self._redis = None
        self._redis_url = redis_url
        self.epoch = 0
        self._is_owner = False
        self._lease_valid_until = 0.0
        self._unhealthy_streak = 0
        self._polling_task = None
        self._reminder_task = None
        self._reminder_stop = asyncio.Event()

    @property
    def is_owner_now(self) -> bool:
        return self._is_owner and time.time() < self._lease_valid_until

    async def run(self, on_acquire, on_release, on_lost):
        """Main loop. Never exits."""
        await self._connect_redis()

        while True:
            try:
                if self._is_owner:
                    await self._owner_tick(on_release, on_lost)
                else:
                    await self._standby_tick(on_acquire)
            except Exception as e:
                logger.error(f"LeaseManager tick error: {e}", exc_info=True)
                if self._is_owner:
                    await self._fail_closed(on_lost)
            await asyncio.sleep(self.RENEW_INTERVAL)

    # --- Owner tick ---

    async def _owner_tick(self, on_release, on_lost):
        # Monitor polling task — if it died, we're not actually serving
        if self._polling_task and self._polling_task.done():
            exc = self._polling_task.exception() if not self._polling_task.cancelled() else None
            logger.error(f"Polling task died: {exc}")
            await self._fail_closed(on_lost)
            return

        health_ok = await self._check_health()
        if health_ok:
            self._unhealthy_streak = 0
        else:
            self._unhealthy_streak += 1

        sessions = self._build_sessions()
        healthy = self._unhealthy_streak < 3

        try:
            result = await self._lua_renew(healthy, sessions)
        except Exception:
            await self._fail_closed(on_lost)
            return

        if result == -1 or result == 0:
            await self._fail_closed(on_lost)
        elif result == 2:
            await self._graceful_release(on_release, on_lost)
        else:
            self._lease_valid_until = time.time() + self.LOCAL_TTL

        if self._unhealthy_streak >= self.HEALTH_RELEASE_STRIKES:
            await self._graceful_release(on_release, on_lost)

    # --- Standby tick ---

    async def _standby_tick(self, on_acquire):
        try:
            epoch = await self._lua_acquire(self._build_sessions())
        except Exception:
            return

        if epoch > 0:
            self.epoch = epoch
            self._is_owner = True
            self._lease_valid_until = time.time() + self.LOCAL_TTL
            self._unhealthy_streak = 0
            sessions = await self._get_sessions_from_lease()
            try:
                await on_acquire(epoch, sessions)
            except Exception as e:
                logger.error(f"on_acquire failed: {e}", exc_info=True)
                # STOP polling if it was started before the failure
                await self._emergency_stop()
                self._is_owner = False
                self._lease_valid_until = 0
                try:
                    await self._lua_release(self._build_sessions())
                except Exception:
                    pass

    # --- Fail closed ---

    async def _fail_closed(self, on_lost):
        """Any error while owning → full stop."""
        self._is_owner = False
        self._lease_valid_until = 0
        await self._emergency_stop()
        try:
            await on_lost()
        except Exception:
            pass

    async def _emergency_stop(self):
        """Stop polling + reminders immediately."""
        if self._polling_task and not self._polling_task.done():
            try:
                # dp.stop_polling() signals the polling task to stop
                await asyncio.wait_for(dp.stop_polling(), timeout=5)
            except Exception:
                self._polling_task.cancel()
        self._reminder_stop.set()

    # --- Graceful release ---

    async def _graceful_release(self, on_release, on_lost):
        """Drain with TTL renewal, then release. On failure → fail_closed."""
        drain_renew = asyncio.create_task(self._drain_renew_loop())
        try:
            await self._lua_start_drain()
            await asyncio.wait_for(on_release(), timeout=self.DRAIN_DEADLINE)
            # Drain succeeded → release lease
            await self._lua_release(self._build_sessions())
            self._is_owner = False
            self._lease_valid_until = 0
        except Exception as e:
            logger.error(f"Graceful release failed: {e}")
            # Could not prove clean drain → DON'T release.
            # Let TTL expire. Other node waits 120s. Safe.
            await self._fail_closed(on_lost)
        finally:
            drain_renew.cancel()
            try:
                await drain_renew
            except asyncio.CancelledError:
                pass

    async def _drain_renew_loop(self):
        """Renew TTL every 15s while draining."""
        try:
            while True:
                await asyncio.sleep(15)
                try:
                    await self._lua_renew(healthy=True, sessions=self._build_sessions())
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
```

### Key Design Choices

1. **on_acquire failure → emergency_stop + release**: If polling started but
   later startup fails, polling is stopped before releasing lease.

2. **_graceful_release failure → DON'T release**: If drain/stop_polling fails,
   we can't prove polling stopped → don't release → lease expires via TTL.
   Other node waits 120s. Safe but slow. Better than split-brain.

3. **drain_renew_loop**: Separate task keeps TTL alive during drain.
   Drain deadline (60s) < TTL (120s) with margin.

## Lua Scripts (5 total, all epoch-guarded)

### 1. Acquire — only when owner==null or key missing

```lua
local raw = redis.call("GET", KEYS[1])
if raw then
    local d = cjson.decode(raw)
    if d.owner ~= cjson.null then return 0 end
end
local epoch = 1
local merged = cjson.decode(ARGV[3])
if raw then
    local d = cjson.decode(raw)
    epoch = (d.epoch or 0) + 1
    if d.sessions then
        for k, v in pairs(d.sessions) do
            if type(v) == "table" and v.ts then
                if not merged[k] or not merged[k].ts or v.ts > merged[k].ts then
                    merged[k] = v
                end
            end
        end
    end
end
local lease = cjson.encode({
    owner = ARGV[1], epoch = epoch, healthy = true,
    draining = false, handback_to = cjson.null,
    sessions = merged, updated_at = tonumber(ARGV[4])
})
redis.call("SET", KEYS[1], lease, "EX", tonumber(ARGV[2]))
return epoch
```

### 2. Renew — owner+epoch guard, session merge, returns handback signal

```lua
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner ~= ARGV[1] or d.epoch ~= tonumber(ARGV[2]) then return -1 end
d.healthy = ARGV[3] == "true"
d.updated_at = tonumber(ARGV[5])
local incoming = cjson.decode(ARGV[4])
for k, v in pairs(incoming) do
    if type(v) == "table" and v.ts then
        if not d.sessions[k] or not d.sessions[k].ts or v.ts >= d.sessions[k].ts then
            d.sessions[k] = v
        end
    end
end
redis.call("SET", KEYS[1], cjson.encode(d), "EX", tonumber(ARGV[6]))
if d.handback_to ~= cjson.null then return 2 end
return 1
```

### 3. Release — owner+epoch guard, final merge, set owner=null

```lua
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner ~= ARGV[1] or d.epoch ~= tonumber(ARGV[2]) then return 0 end
local incoming = cjson.decode(ARGV[3])
for k, v in pairs(incoming) do
    if type(v) == "table" and v.ts then
        if not d.sessions[k] or not d.sessions[k].ts or v.ts >= d.sessions[k].ts then
            d.sessions[k] = v
        end
    end
end
d.owner = cjson.null; d.draining = false; d.handback_to = cjson.null
d.updated_at = tonumber(ARGV[4])
redis.call("SET", KEYS[1], cjson.encode(d), "EX", tonumber(ARGV[5]))
return 1
```

### 4. Request Handback — KEEPTTL (no TTL extension)

```lua
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner == ARGV[1] then return 0 end
if d.owner == cjson.null then return -1 end
d.handback_to = ARGV[1]
redis.call("SET", KEYS[1], cjson.encode(d), "KEEPTTL")
return d.epoch
```

### 5. Start Drain — KEEPTTL, owner+epoch guard

```lua
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner ~= ARGV[1] or d.epoch ~= tonumber(ARGV[2]) then return 0 end
d.draining = true
redis.call("SET", KEYS[1], cjson.encode(d), "KEEPTTL")
return 1
```

## Health Check

```python
async def _check_health(self) -> bool:
    try:
        proxy = os.environ.get("HTTPS_PROXY")
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as s:
            kw = {"timeout": timeout}
            if proxy:
                kw["proxy"] = proxy
            async with s.get("https://api.anthropic.com/", **kw) as r:
                return r.status < 500
    except Exception:
        return False
```

Hysteresis: 3 fails → unhealthy in lease. 6 fails → voluntary release.

## ChatState Shutdown — ALL Public Methods

```python
# _shutdown flag checked at entry of EVERY public method:

async def accept_entry(self, entry):
    async with self._lock:
        if self._shutdown: return
        ...

async def transcription_started(self):
    async with self._lock:
        if self._shutdown: return (self.generation, self.media_generation)
        ...

async def transcription_finished(self, entry, gen, media_gen):
    async with self._lock:
        if self._shutdown:
            self.pending_transcriptions = max(0, self.pending_transcriptions - 1)
            return
        ...

async def request_stop(self):
    async with self._lock:
        if self._shutdown: return False
        ...

async def request_clear(self):
    async with self._lock:
        if self._shutdown: return False
        ...

async def request_compact(self):
    async with self._lock:
        if self._shutdown: return False
        ...

async def run_urgent_prompt(self, prompt):
    async with self._lock:
        if self._shutdown: return
        ...

async def set_model(self, model_id, use_1m):
    async with self._lock:
        if self._shutdown: return
        ...

async def set_debounce(self, seconds):
    async with self._lock:
        if self._shutdown: return
        ...

async def _drain_or_idle(self):
    async with self._lock:
        if self._shutdown:
            self.phase = ChatPhase.IDLE
            return
        ...

async def enter_shutdown(self):
    async with self._lock:
        self._shutdown = True
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
    if self._processing_task and not self._processing_task.done():
        try:
            await asyncio.wait_for(self._processing_task, timeout=60)
        except asyncio.TimeoutError:
            self._processing_task.cancel()
            try: await self._processing_task
            except: pass

async def exit_shutdown(self):
    async with self._lock:
        self._shutdown = False
```

## Drain Orchestrator

```python
async def drain_for_handback(registry, lease_manager):
    """on_release callback. drain_renew_loop runs concurrently (LeaseManager manages it)."""
    # 1. Stop polling
    await dp.stop_polling()

    # 2. Shutdown all chats CONCURRENTLY (must wrap coroutines in tasks)
    tasks = [asyncio.create_task(cs.enter_shutdown()) for cs in registry._chats.values()]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=55)
        for t in pending:
            t.cancel()

    # 3. Stop reminders
    lease_manager._reminder_stop.set()
    if lease_manager._reminder_task and not lease_manager._reminder_task.done():
        try:
            await asyncio.wait_for(lease_manager._reminder_task, timeout=5)
        except asyncio.TimeoutError:
            lease_manager._reminder_task.cancel()

    # 4. Push bot repo commits (if any made during failover)
    bot_repo = Path(__file__).parent.resolve()
    await _push_repo(str(bot_repo), "kesha-bot")

    # Sessions pushed by LeaseManager after this returns

async def _push_repo(path: str, name: str):
    """Best-effort push. Failures logged but don't block drain."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "push", "--quiet",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            logger.info(f"{name}: pushed OK")
    except Exception as e:
        logger.warning(f"{name}: push failed (non-critical): {e}")
```

## Bot Integration — ALL Startup Gated, Polling LAST

```python
async def main():
    if KESHA_REDIS_URL:
        lease = LeaseManager(KESHA_NODE_ID, KESHA_REDIS_URL)

        # Install transport fence BEFORE anything else
        bot.session.middleware(LeaseGateMiddleware(lease))

        async def on_acquire(epoch, sessions):
            # 1. Sync sessions (no TG sends)
            await registry.sync_from_lease(sessions)
            for cs in registry._chats.values():
                await cs.exit_shutdown()

            # 2. Deliver missed reminders (fenced — suppressed if lease lost)
            await deliver_missed_on_startup(bot, registry, ALLOWED)

            # 3. Start reminder loop
            lease._reminder_stop.clear()
            lease._reminder_task = asyncio.create_task(
                reminder_loop(bot, registry, ALLOWED, lease)
            )

            # 4. Greet (fenced)
            await bot.send_message(NOTIFY_CHAT,
                f"🔄 {KESHA_NODE_ID} online (epoch {epoch})")

            # 5. Start polling LAST (after all fallible actions)
            lease._polling_task = asyncio.create_task(dp.start_polling(bot))

        async def on_release():
            await drain_for_handback(registry, lease)

        async def on_lost():
            for cs in registry._chats.values():
                await cs.enter_shutdown()

        await lease.run(on_acquire, on_release, on_lost)
    else:
        await dp.start_polling(bot)
```

### Polling LAST

Polling starts as the LAST action in on_acquire. If any prior step fails,
on_acquire raises → LeaseManager catches → emergency_stop (no polling to stop
because it wasn't started yet) → release lease.

## Session Timestamps

```python
# claude_session.py:
self.session_id_changed_at = 0

# On ResultMessage:
self.session_id = msg.session_id
self.session_id_changed_at = int(time.time())

# build_sessions_payload:
result[str(chat_id)] = {"id": cs.session.session_id, "ts": cs.session.session_id_changed_at}

# sync_from_lease — preserve ts:
cs.session.session_id_changed_at = info["ts"]
```

## Epoch Guard Middleware

```python
@dp.update.outer_middleware()
async def epoch_guard(handler, event, data):
    if lease and not lease.is_owner_now:
        return  # drop silently
    return await handler(event, data)
```

## Reminders — Ownership-Gated

```python
async def reminder_loop(bot, registry, allowed, lease):
    db = get_db()
    while not lease._reminder_stop.is_set():
        if lease and not lease.is_owner_now:
            pass  # skip tick
        else:
            # ... existing logic (bot is fenced at transport level) ...
            pass
        try:
            await asyncio.wait_for(lease._reminder_stop.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
```

## COG (Working Directory) Sync

Kesha's CWD on laptop is `COG-second-brain/` — user's personal knowledge base,
projects, docs, notes. VPS needs access to serve as a real backup.

### Auto-Push on Laptop (cron)

```bash
# /etc/cron.d/cog-auto-push (laptop)
*/5 * * * * maxim cd "/mnt/data/Рабочий стол/Cursor/COG-second-brain" && \
  git add -A && \
  git diff --cached --quiet || \
  (git commit -m "auto-sync $(date +\%Y-\%m-\%d\ \%H:\%M)" --no-verify && \
  git push --quiet) 2>/dev/null
```

Every 5 min: stage all → commit if changes → push.
Worst case: last 5 min of edits not on VPS.
`.gitignore` left as-is — user manages what's tracked.

### Auto-Pull on VPS (on failover)

```python
# In on_acquire callback, before starting polling:
async def _sync_repo(path: str, name: str):
    """Force-sync to remote: fetch + reset --hard. Laptop always wins.
    git clean excludes local config files (.mcp.json, .env, secrets)."""
    for cmd in [
        ["git", "-C", path, "fetch", "--quiet"],
        ["git", "-C", path, "reset", "--hard", "origin/main"],
        ["git", "-C", path, "clean", "-fd", "-e", ".mcp.json", "-e", ".env", "-e", "*.secret"],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            logger.warning(f"{name} sync failed at {cmd[2]}")
            return False
    logger.info(f"{name} sync: OK")
    return True

# Called in on_acquire:
BOT_REPO = str(Path(__file__).parent.resolve())  # works on both laptop and VPS
await _sync_repo(WORK_DIR, "COG")
await _sync_repo(BOT_REPO, "kesha-bot")
```

### COG on VPS

```bash
# One-time clone:
git clone git@github.com:<user>/COG-second-brain.git /opt/cog-second-brain

# WORK_DIR in .env.vps:
WORK_DIR=/opt/cog-second-brain
```

VPS Kesha works in the SAME directory structure as laptop.
Claude can read/edit files, access projects, notes — full context.

### Conflict Resolution

Laptop always wins. VPS sync uses `git fetch && git reset --hard origin/main`
which discards any local VPS changes and forces to match remote.

VPS Claude CAN write files during failover (Claude tools: Write, Edit).
These writes are local-only. On next sync (handback → laptop takes over →
laptop pushes → VPS next acquire → `reset --hard`) they're overwritten.

If VPS Claude creates important files → it should commit + push to kesha-tg-bot
repo (not COG), or notify user to save manually.

### Bi-Directional Sync (kesha-tg-bot repo)

Both nodes may commit to the bot repo (code changes, config).
On failover, VPS pulls latest. On handback, laptop pulls latest.

```python
# In on_acquire, sync BOTH repos:
await _sync_repo(WORK_DIR, "COG")           # user's knowledge base
await _sync_repo("/opt/kesha-bot", "kesha-bot")  # bot code
```

Auto-push from VPS (for bot commits made during failover):
```bash
# In on_release (drain), before releasing lease:
async def _push_bot_repo():
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", "/opt/kesha-bot", "push", "--quiet",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await asyncio.wait_for(proc.communicate(), timeout=15)
```

## Global Claude Rules on VPS

Kesha uses Claude Agent SDK which reads:
- `~/.claude/CLAUDE.md` — global user rules
- `<project>/CLAUDE.md` — project rules (in COG and kesha-tg-bot repos)
- `~/.claude/settings.json` — permissions, MCP configs

### Setup on VPS

```bash
# Create service user
sudo useradd -m -s /bin/bash kesha
sudo mkdir -p /home/kesha/.claude/docs
sudo mkdir -p /home/kesha/.claude/mcp-servers
sudo mkdir -p /home/kesha/.claude/projects/-opt-cog-second-brain/memory/

# Copy global CLAUDE.md from laptop (one-time, update on changes)
scp laptop:~/.claude/CLAUDE.md /home/kesha/.claude/CLAUDE.md

# Copy global docs
scp -r laptop:~/.claude/docs/ /home/kesha/.claude/docs/

# Copy project-specific memory
scp -r laptop:~/.claude/projects/-mnt-data-*-COG-second-brain/memory/* \
  /home/kesha/.claude/projects/-opt-cog-second-brain/memory/

# VPS-SPECIFIC settings.json — minimal, WITHOUT aperant/kwin MCP paths
# DO NOT copy laptop's settings.json directly — it has laptop-specific paths/perms
cat > /home/kesha/.claude/settings.json << 'SETTINGS'
{
  "permissions": {
    "allow": ["Bash(*)","Read(*)","Write(*)","Edit(*)","WebSearch(*)","WebFetch(*)"]
  }
}
SETTINGS

# Set ownership + permissions
sudo chown -R kesha:kesha /home/kesha/.claude /opt/kesha-bot /opt/cog-second-brain
sudo find /home/kesha/.claude -type d -exec chmod 700 {} +
sudo find /home/kesha/.claude -type f -exec chmod 600 {} +
```

### Sync Strategy

Global rules change rarely. Manual `scp` or git-tracked dotfiles repo.
No auto-sync needed — update after significant rule changes.

Project CLAUDE.md lives in repos → synced via `git pull` automatically.

## MCP Tools on VPS

| MCP Server | On VPS | Config | Notes |
|------------|--------|--------|-------|
| **kesha** | ✅ | Built into bot | Bot's own tools |
| **websearch** | ✅ | API key in env | Perplexity, works anywhere |
| **yougile** | ✅ | API key in env | Task tracker, API-based |
| **gmail** | ✅ | OAuth tokens | Copy token files from laptop |
| **mailru** | ✅ | API key in env | Mail.ru API |
| **mcp-pandoc** | ✅ | `apt install pandoc` | Document conversion |
| **aperant** | ❌ | — | Smart home, local network only |
| **kwin** | ❌ | — | Desktop control, meaningless on VPS |

### VPS MCP Config

VPS uses its own `.mcp.json` in WORK_DIR (`/opt/cog-second-brain/.mcp.json`).
This file is NOT synced via git — it's VPS-local. Create manually on VPS,
exclude aperant + kwin, keep everything else.

The current `_load_global_mcp()` in `bot.py` already reads `WORK_DIR/.mcp.json`.
Since VPS has `WORK_DIR=/opt/cog-second-brain`, it reads the VPS-specific file.
No code changes needed — just place the right `.mcp.json` on VPS.

```bash
# On VPS — create .mcp.json without aperant/kwin:
# Copy laptop's, then remove aperant + kwin entries:
scp laptop:"/mnt/data/Рабочий стол/Cursor/COG-second-brain/.mcp.json" \
  /opt/cog-second-brain/.mcp.json
# Edit: remove "aperant" and "kwin" server entries

# IMPORTANT: add .mcp.json to COG's .gitignore or make it untracked
# so laptop's auto-push doesn't overwrite VPS version:
cd /opt/cog-second-brain && git update-index --skip-worktree .mcp.json
```

VPS `.mcp.json` = laptop's minus aperant + kwin. Same API keys via env/config.

### Secrets & Credentials Transfer (via SSH)

Secrets that are NOT in git (API keys, OAuth tokens, creds files) — transfer
once via SSH. Update manually when they change.

```bash
# On VPS — pull secrets from laptop:

# 1. Bot .env (API keys, tokens)
scp laptop:/mnt/data/Projects/Python/kesha-tg-bot/.env /opt/kesha-bot/.env
# Then edit: remove HTTPS_PROXY, set KESHA_NODE_ID=vps, KESHA_REDIS_URL=localhost

# 2. MCP server credentials
scp -r laptop:~/.claude/mcp-servers/ /home/kesha/.claude/mcp-servers/

# 3. Gmail OAuth tokens (if using gmail MCP)
scp -r laptop:~/.claude/mcp-configs/ /home/kesha/.claude/mcp-configs/

# 4. Any COG secrets (not in git)
scp laptop:"/mnt/data/Рабочий стол/Cursor/COG-second-brain/.env" \
  /opt/cog-second-brain/.env 2>/dev/null || true

# 5. Permissions
sudo chown -R kesha:kesha /home/kesha/.claude /opt/kesha-bot /opt/cog-second-brain
sudo chmod 600 /opt/kesha-bot/.env /opt/cog-second-brain/.env 2>/dev/null || true
```

### MCP Credential Files on VPS

```bash
# Gmail OAuth (one-time copy):
scp -r laptop:~/.claude/mcp-servers/gmail/ /home/kesha/.claude/mcp-servers/gmail/

# Websearch config:
scp laptop:~/.claude/mcp-servers/websearch/config.json \
  /home/kesha/.claude/mcp-servers/websearch/config.json
```

## Deployment

### Redis on VPS
```bash
sudo apt install redis-server
# /etc/redis/redis.conf:
#   bind 0.0.0.0
#   requirepass <strong_password>
# sudo ufw allow from <laptop-ip> to any port 6379
```

### VPS Full Setup

```bash
# 0. Service user + directories
sudo useradd -m -s /bin/bash kesha
sudo install -d -o kesha -g kesha -m 700 /home/kesha/.ssh
sudo install -d -o kesha -g kesha /opt/kesha-bot /opt/cog-second-brain

# 1. SSH deploy key for git repos (as kesha user)
sudo -u kesha ssh-keygen -t ed25519 -N "" -f /home/kesha/.ssh/id_ed25519
# Add /home/kesha/.ssh/id_ed25519.pub as deploy key on GitHub repos
# (kesha-tg-bot + COG-second-brain, read-write access)
sudo -u kesha bash -c 'echo "Host github.com
  StrictHostKeyChecking accept-new
  IdentityFile ~/.ssh/id_ed25519" > ~/.ssh/config'

# 2. Bot repo
sudo -u kesha git clone git@github.com:DrSeedon/kesha-tg-bot.git /opt/kesha-bot
cd /opt/kesha-bot && sudo -u kesha python3 -m venv .venv
sudo -u kesha .venv/bin/pip install -r requirements.txt
sudo -u kesha .venv/bin/pip install redis

# 3. COG repo
sudo -u kesha git clone git@github.com:<user>/COG-second-brain.git /opt/cog-second-brain

# 4. System tools
sudo apt install pandoc ffmpeg redis-server

# 5. Claude global config (run from laptop):
#    scp -r ~/.claude/CLAUDE.md vps:/home/kesha/.claude/
#    scp -r ~/.claude/docs/ vps:/home/kesha/.claude/docs/
#    scp -r ~/.claude/mcp-servers/ vps:/home/kesha/.claude/mcp-servers/
#    scp -r ~/.claude/mcp-configs/ vps:/home/kesha/.claude/mcp-configs/

# 6. VPS-specific settings.json (minimal, no aperant/kwin)
sudo -u kesha bash -c 'cat > /home/kesha/.claude/settings.json << SETTINGS
{
  "permissions": {
    "allow": ["Bash(*)","Read(*)","Write(*)","Edit(*)","WebSearch(*)","WebFetch(*)"]
  }
}
SETTINGS'

# 7. .env (copy from laptop, edit)
#    scp laptop:/mnt/data/Projects/Python/kesha-tg-bot/.env /opt/kesha-bot/.env
#    Edit: remove HTTPS_PROXY, set:
#      KESHA_NODE_ID=vps
#      KESHA_REDIS_URL=redis://:password@localhost:6379/0
#      WORK_DIR=/opt/cog-second-brain

# 8. VPS-specific .mcp.json in COG (without aperant/kwin)
#    scp laptop:"COG-second-brain/.mcp.json" /opt/cog-second-brain/.mcp.json
#    Edit: remove aperant + kwin entries
#    Then: cd /opt/cog-second-brain && git update-index --skip-worktree .mcp.json

# 9. Permissions
sudo chown -R kesha:kesha /home/kesha /opt/kesha-bot /opt/cog-second-brain
sudo chmod 600 /opt/kesha-bot/.env

# 10. Systemd
sudo bash -c 'cat > /etc/systemd/system/kesha-bot-vps.service << SERVICE
[Unit]
Description=Kesha Telegram Bot (VPS failover)
After=network.target redis.target

[Service]
Type=simple
User=kesha
WorkingDirectory=/opt/kesha-bot
ExecStart=/opt/kesha-bot/.venv/bin/python3 bot.py
EnvironmentFile=/opt/kesha-bot/.env
Environment=HOME=/home/kesha
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE'
sudo systemctl daemon-reload
sudo systemctl enable kesha-bot-vps
sudo systemctl start kesha-bot-vps
```

### Laptop .env additions
```bash
KESHA_NODE_ID=laptop
KESHA_REDIS_URL=redis://:password@<vps-domain>:6379/0
```

### Laptop cron (COG auto-push)
```bash
crontab -e
# Add:
*/5 * * * * cd "/mnt/data/Рабочий стол/Cursor/COG-second-brain" && git add -A && git diff --cached --quiet || (git commit -m "auto-sync $(date +\%Y-\%m-\%d\ \%H:\%M)" --no-verify && git push --quiet) 2>/dev/null
```

## Phases

1. **LeaseManager + Lua** (~350 lines): failover.py
2. **Transport fence + bot integration** (~100 lines): LeaseGateMiddleware, bot.py
3. **ChatState shutdown + drain** (~100 lines): all methods, drain orchestrator
4. **Session sync** (~60 lines): timestamps, build/sync payloads
5. **VPS deploy + COG + rules + MCP** (~80 lines): Redis, COG clone, claude config, MCP setup, systemd, cron

**Total: ~690 lines + deployment configs**

## Resolution Matrix — ALL Findings v1-v6

| Finding | Resolution |
|---------|-----------|
| v1: Split-brain | ✅ Lease + epoch + local deadline |
| v1: Wrong health | ✅ Claude probe + 6-strike + voluntary release |
| v1: Startup ungated | ✅ ALL in on_acquire, polling LAST |
| v2: Redis fallback | ✅ No fallback ever |
| v2: Non-atomic | ✅ 5 Lua scripts |
| v2: Handback unsafe | ✅ drain_renew_loop + deadline + parallel |
| v3: Standalone | ✅ Removed |
| v3: Unhealthy keeps lease | ✅ Voluntary release |
| v3: Acquire during drain | ✅ Only owner==null |
| v3: Session erasure | ✅ Lua merge with ts |
| v4: request_handback TTL | ✅ KEEPTTL |
| v4: Drain > TTL | ✅ drain_renew_loop + 60s < 120s |
| v4: In-flight sends | ✅ Transport-level middleware |
| v4: Session ts=now() | ✅ session_id_changed_at |
| v4: on_acquire blocks | ✅ Polling as create_task |
| v4: Shutdown incomplete | ✅ ALL methods listed |
| v5: Callback kills renewal | ✅ fail_closed on ANY exception |
| v5: Stale is_owner | ✅ Local deadline (15s max) |
| v5: Drain blocks renewal | ✅ Separate drain_renew task |
| v5: MCP tools unfenced | ✅ Transport middleware catches ALL |
| **v6: on_acquire fail → polling leaked** | ✅ **Polling LAST + emergency_stop in fail path** |
| **v6: graceful_release without proof** | ✅ **Drain fail → DON'T release, let TTL expire** |
| **v6: FencedBot misses send_photo/answer** | ✅ **Replaced with aiogram session middleware** |
| **v7: Middleware wrong signature** | ✅ **`(make_request, bot, method)` — correct aiogram sig** |
| **v7: GetUpdates allowlisted** | ✅ **Block ALL calls including GetUpdates when not owner** |
| **v7: polling_task unmonitored** | ✅ **Owner tick checks `_polling_task.done()` → fail_closed** |
| **v7: asyncio.wait(coroutines)** | ✅ **Wrapped in `create_task()`** |

## FAQ — Context, Motivation, Edge Cases

### What is this project?

Kesha is a Telegram bot wrapping Claude Agent SDK (`claude-agent-sdk` Python lib).
Users write to a Telegram bot, messages go to Claude (Anthropic's AI), Claude
responds with text/tool calls. The bot handles streaming, message batching
(debounce), media transcription, reminders, and MCP tool execution.

Codebase: ~3000 lines Python, aiogram 3.27, asyncio-based.

### What is Claude Agent SDK?

Official Anthropic SDK for persistent Claude sessions. Key concepts:
- `ClaudeSDKClient` — persistent WebSocket connection to Claude
- `session_id` — string key to resume a conversation (context stored on Anthropic servers)
- `send_message()` → async generator yielding stream events (text deltas, tool calls)
- `query()` + `receive_messages()` — lower-level: inject mid-conversation, receive results
- `connect()` / `disconnect()` — manage WebSocket lifecycle
- Works through HTTPS proxy (laptop uses Hiddify VPN proxy to reach Anthropic API)

### Why do we need failover?

Owner works remotely (dacha/country house) with unstable mobile WiFi tethering.
The laptop runs the bot but can't reliably reach Claude API through the VPN proxy.
Result: bot sends message to Claude → 45 second timeout → empty response → user
gets nothing. This happens repeatedly, making the bot unusable.

VPS has stable internet and direct API access (no proxy needed). But we don't
want to move the bot permanently to VPS because:
- Laptop has full file access (user's projects, documents, knowledge base)
- Laptop has all MCP tools (smart home, desktop control)
- Laptop is the primary development machine
- When home internet is stable, laptop works perfectly

### Why not just run on VPS permanently?

VPS has limited capabilities:
- No access to user's local files, desktop, smart home
- No aperant MCP (home automation) or kwin MCP (desktop control)
- Would need constant sync of all files (impractical for large projects)
- User prefers laptop as primary — VPS is backup only

### Why not webhook instead of polling?

Webhook would make VPS the single ingress point → VPS down = everything down.
Polling allows both nodes to be fully independent. The tradeoff (more complex
leader election) is worth it for the independence guarantee.

### What is `session_id` and why does it need syncing?

`session_id` is a string like `"sess_abc123..."` stored locally in
`./storage/sessions/<chat_id>`. It's a key to resume the Claude conversation
on Anthropic's servers — all context (messages, tool results, system prompt)
is stored remotely. By syncing session_id, the VPS can continue the exact
same conversation the laptop was having.

Without sync: VPS starts a fresh conversation, Claude has no context.
With sync: VPS resumes where laptop left off, Claude remembers everything.

### What is COG-second-brain?

User's personal knowledge base — a git repository with:
- Personal notes and documents
- Project files and resources
- Work materials and references

This is the CWD (working directory) where Claude operates. Claude can read/write
files here via tools (Read, Write, Edit, Bash). If VPS doesn't have COG,
Claude on VPS can't access any of the user's files.

### What is ChatState and why does it have phases?

ChatState is a per-chat state machine managing the message lifecycle:
```
IDLE → COLLECTING (debounce) → PROCESSING (Claude working) → IDLE
```

Key concepts:
- **Debounce**: when user sends multiple messages quickly, they're batched into one
  Claude request (default 3 second wait)
- **Inject**: if user sends a message while Claude is processing, it's injected
  into the active conversation (not queued for next turn)
- **Deferred**: messages that arrive during STOPPING/COMPACTING are queued and
  processed after the current operation finishes

Shutdown mode (`_shutdown=True`) prevents all state transitions and rejects
new entries — needed for clean drain during failover.

### What are MCP tools?

Model Context Protocol — Claude can use external tools:
- **kesha**: bot's own tools (set_model, send_photo, reminders, etc.)
- **websearch**: web search via Perplexity API
- **yougile**: task tracker (Yougile API)
- **gmail/mailru**: email access
- **aperant**: smart home control (local network only — laptop only)
- **kwin**: KDE desktop control (laptop only)
- **mcp-pandoc**: document conversion

VPS gets all API-based tools. Laptop-only tools (aperant, kwin) excluded from VPS config.

### Why Redis and not simpler coordination?

Tried and rejected (Codex review rounds 1-3):
- **Simple heartbeat**: doesn't prove "can respond to users", only "process alive"
- **File-based lock**: no shared filesystem between nodes
- **Git-based flag**: too slow, race conditions on push
- **Telegram-based check**: API doesn't expose "who is polling"

Redis provides:
- Atomic operations (Lua scripts)
- Key expiry (TTL — automatic lease release on crash)
- Low latency (<1ms local, <50ms remote)
- Proven distributed lock patterns (Redlock-like)

### Why Lua scripts and not plain Redis commands?

Atomicity. Example race without Lua:
1. Node A: `GET lease` → sees owner=null
2. Node B: `GET lease` → sees owner=null
3. Node A: `SET lease owner=A` ← both think they acquired
4. Node B: `SET lease owner=B` ← B overwrites A silently

With Lua: entire check+modify+write runs as one atomic operation on Redis server.
No interleaving possible.

### What is epoch fencing?

Epoch = monotonically increasing integer. Every lease acquisition increments it.
If a node discovers the current epoch is higher than its own → it lost ownership.

This prevents the "stale owner" problem:
- Node A owns lease (epoch=5), network glitch
- Node B acquires (epoch=6)
- Node A reconnects, tries to renew with epoch=5
- Redis: epoch 5 < 6 → reject → Node A knows it lost

### What is the local lease deadline?

Problem: `is_owner` flag is only updated every 30s (renewal interval).
Between renewals, the flag can be stale.

Solution: `is_owner_now` checks `time.time() < _lease_valid_until`.
`_lease_valid_until` is set to `now + 45s` on each successful renewal.
If renewal fails (any reason), the deadline expires within 15 seconds,
and all side effects are suppressed automatically.

### What happens if both nodes start simultaneously?

Redis `acquire` Lua script: first writer wins (atomic check+write).
Loser's acquire returns 0 → stays in standby mode.

### What happens if Redis crashes?

- **VPS**: Redis is local, VPS bot can't renew → stops polling. Bot is dead
  until Redis restarts.
- **Laptop**: Can't reach Redis → stops polling (safety rule). Both nodes stopped.
  When Redis comes back → first to acquire wins.

No split-brain: both stop when they can't prove ownership.

### What happens during a 5-second network glitch?

- Renewal is every 30s. A 5s glitch likely falls between renewals → no effect.
- If glitch hits during renewal → renewal fails → next renewal in 30s succeeds.
- Local deadline is 45s → plenty of margin.
- If glitch is longer than 45s → local deadline expires → side effects suppressed.
  But lease hasn't expired yet (TTL=120s) → no failover. Node waits, retries.

### What happens if laptop loses internet completely?

- Laptop can't renew lease → local deadline expires (15s) → stops all sends
- Lease TTL expires (120s) → VPS acquires → starts polling
- Total failover time: ~135s (15s local + 120s TTL expiry)
- If laptop had time before dying: voluntary release (health check fails 6x in 180s) → faster

### What happens if Claude is down for everyone?

Both nodes: health check fails → `healthy=false` in lease → after 180s voluntary release.
VPS acquires → same health check fails → VPS also unhealthy → voluntary release.
Ping-pong until Claude comes back. Each node holds for 180s before releasing.

To prevent ping-pong: standby should also check health before acquiring.
Add health check in `_standby_tick` before `_lua_acquire`.

### What if user creates a reminder on laptop, then laptop fails over to VPS?

v1 (current plan): Reminder is in laptop's local SQLite. VPS doesn't know about it.
If the reminder fires while VPS is active → it doesn't fire (VPS has no record).
When laptop comes back → reminder fires (if not expired).

v2 (future): Reminders in Redis → shared between both nodes.

### What about media files (photos, voice, documents)?

Media is downloaded to `./storage/media/` locally. NOT synced between nodes.
If VPS needs a file that was on laptop → it downloads fresh from Telegram.
Media cache is per-node. This is fine — Telegram stores files for ~1 hour.

### What about voice transcription on VPS?

VPS needs: `DEEPGRAM_API_KEY` in .env + `ffmpeg` installed.
Same Deepgram key works from any IP. ffmpeg: `sudo apt install ffmpeg`.

### How do I update the bot code on both nodes?

```bash
# On laptop: make changes, commit, push
git add -A && git commit -m "..." && git push

# On VPS: pull (automatic on next failover, or manual)
sudo -u kesha git -C /opt/kesha-bot pull
sudo systemctl restart kesha-bot-vps
```

If VPS is currently active (has lease), it will pull on next handback.
For urgent updates: manually restart VPS service (it will re-acquire after restart).

### Can I force failover for testing?

```bash
# On laptop, stop the bot:
sudo systemctl stop kesha-bot
# Lease expires in 120s → VPS takes over

# Or, faster — manually release via redis-cli on VPS:
redis-cli -a <password> DEL kesha:lease
# VPS acquires immediately on next tick (30s)
```

### What are the costs?

- **Redis**: negligible (tiny key, few ops/minute)
- **VPS**: already have one (existing server with domain)
- **Claude API**: same key, same billing. No double-charging — only one node active.
- **Telegram**: free (Bot API)

### What if I want to disable failover?

Remove `KESHA_REDIS_URL` from laptop .env → bot runs in solo mode (no failover).
Stop VPS service: `sudo systemctl stop kesha-bot-vps`.

### What are the known limitations?

1. **Failover time**: 120-180 seconds (lease TTL + health hysteresis)
2. **Last 5 min of COG edits**: may not reach VPS (cron sync interval)
3. **Reminders**: not shared between nodes (v1, local SQLite)
4. **In-flight messages on crash**: lost (debounce batch, active transcription)
5. **VPS has no aperant/kwin**: smart home and desktop control unavailable
6. **VPS Claude responses**: may differ slightly (different CWD state, fewer tools)
7. **Health check ping-pong**: if Claude API is down globally, nodes trade lease

### What is the `HTTPS_PROXY` mentioned in the plan?

Laptop uses Hiddify VPN proxy (`http://127.0.0.1:12334`) to reach Anthropic API
(blocked in Russia). VPS has direct access (hosted outside Russia).
VPS .env does NOT set HTTPS_PROXY — it connects directly.

### What is the bot's Telegram token situation?

Both nodes use the SAME bot token. Telegram allows only one active poller per token.
If two poll simultaneously → messages split randomly (split-brain).
The entire lease system exists to prevent this.

### What Python version is required?

Python 3.10+ (for `asyncio.TaskGroup`, `StrEnum`, type hints).
Currently running 3.13 on laptop. VPS should match or be ≥3.10.

### What is the VPS server?

User has a VPS at 194.87.250.243 with:
- Domain configured
- Nginx
- Marzban VPN (VLESS+Reality)
- Ubuntu/Debian

Redis, bot deployment, and COG clone go here.

### What is aiogram's session middleware?

aiogram 3.x routes ALL outgoing Telegram API calls through `bot.session`.
A middleware on `bot.session` intercepts every call:
`send_message`, `send_photo`, `edit_message_text`, `set_message_reaction`,
even `message.answer()` (which internally calls `bot.send_message`).

Our `LeaseGateMiddleware` returns `None` for any call when `is_owner_now=False`.
This is the ONLY fencing mechanism needed — no per-call-site patches.

Correct aiogram 3.27 signature: `async def __call__(self, make_request, bot, method)`.

### What happens to typing indicators during failover?

`typing_loop` sends `ChatAction.TYPING` every 4 seconds. During failover:
- LeaseGateMiddleware blocks `send_chat_action` when not owner → typing stops
- No visible effect to user — typing just disappears

### Design decisions log

| Decision | Why | Alternatives considered |
|----------|-----|----------------------|
| Safety > availability | Split-brain is worse than 2 min downtime | Standalone mode (rejected: split-brain) |
| Redis for coordination | Atomic ops, TTL, proven patterns | Git flags (slow), heartbeat (wrong signal), file locks (no shared FS) |
| Lua scripts | Atomicity for multi-step mutations | Plain Redis commands (race conditions) |
| Local lease deadline | Eliminate stale ownership window | Per-update Redis check (too expensive), flag-only (30s stale) |
| aiogram session middleware | Catches ALL TG sends including message.answer() | FencedBot wrapper (misses send_photo, answer bypasses it) |
| Polling LAST in on_acquire | If earlier steps fail, no polling to leak | Polling first (leaked on failure) |
| Don't release on drain failure | Can't prove polling stopped → let TTL expire | Always release (split-brain risk) |
| COG sync via git | Simple, uses existing infra | rsync (complex), NFS (latency), S3 (overkill) |
| Laptop always wins on COG conflicts | Laptop is primary, VPS is backup | Merge (complex), VPS wins (wrong priority) |
| VPS-local .mcp.json | Different tool set than laptop | Shared config (would include aperant/kwin) |
| Secrets via SSH | Not in git, explicit transfer | Git-crypt (complex), Vault (overkill) |
