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

    # Sessions pushed by LeaseManager after this returns
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
  git commit -m "auto-sync $(date +\%Y-\%m-\%d\ \%H:\%M)" --no-verify && \
  git push --quiet 2>/dev/null
```

Every 5 min: stage all → commit if changes → push. Silent, no-verify (skip hooks).
Worst case: last 5 min of edits not on VPS.

### Auto-Pull on VPS (on failover)

```python
# In on_acquire callback, before starting polling:
async def _sync_cog():
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", WORK_DIR, "pull", "--ff-only", "--quiet",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode == 0:
        logger.info("COG sync: git pull OK")
    else:
        logger.warning(f"COG sync: git pull failed: {stderr.decode()}")
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

Laptop always wins. VPS never commits to COG (read-only + Claude tool writes).
If VPS Claude writes a file during failover → it stays local on VPS, not pushed.
When laptop takes back → next auto-push overwrites.

If VPS needs to save something permanent: commit to kesha-tg-bot repo, not COG.

## Global Claude Rules on VPS

Kesha uses Claude Agent SDK which reads:
- `~/.claude/CLAUDE.md` — global user rules
- `<project>/CLAUDE.md` — project rules (in COG and kesha-tg-bot repos)
- `~/.claude/settings.json` — permissions, MCP configs

### Setup on VPS

```bash
# Create claude config directory
mkdir -p /home/kesha/.claude

# Copy global CLAUDE.md from laptop (one-time, update on changes)
scp laptop:~/.claude/CLAUDE.md /home/kesha/.claude/CLAUDE.md

# Copy global docs
scp -r laptop:~/.claude/docs/ /home/kesha/.claude/docs/

# Copy project-specific memory
mkdir -p "/home/kesha/.claude/projects/-opt-cog-second-brain/memory/"
scp -r laptop:~/.claude/projects/-mnt-data-*-COG-second-brain/memory/* \
  /home/kesha/.claude/projects/-opt-cog-second-brain/memory/

# Settings (permissions, MCP)
scp laptop:~/.claude/settings.json /home/kesha/.claude/settings.json
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

```bash
# /opt/cog-second-brain/.mcp.json (VPS version — without aperant, kwin)
# OR environment-specific: .mcp.vps.json loaded via KESHA_MCP_CONFIG env var
```

```python
# config.py addition:
MCP_CONFIG = os.getenv("KESHA_MCP_CONFIG", ".mcp.json")

# bot.py: _load_global_mcp() uses MCP_CONFIG instead of hardcoded ".mcp.json"
```

VPS `.mcp.json` = laptop's minus aperant + kwin. Same API keys via `.env`.

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
# 1. Bot repo
git clone git@github.com:DrSeedon/kesha-tg-bot.git /opt/kesha-bot
cd /opt/kesha-bot && python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. COG repo
git clone git@github.com:<user>/COG-second-brain.git /opt/cog-second-brain

# 3. Claude global config
mkdir -p /home/kesha/.claude/docs
scp laptop:~/.claude/CLAUDE.md /home/kesha/.claude/
scp -r laptop:~/.claude/docs/ /home/kesha/.claude/docs/
scp laptop:~/.claude/settings.json /home/kesha/.claude/

# 4. MCP credentials
mkdir -p /home/kesha/.claude/mcp-servers
scp -r laptop:~/.claude/mcp-servers/gmail/ /home/kesha/.claude/mcp-servers/
scp -r laptop:~/.claude/mcp-servers/websearch/ /home/kesha/.claude/mcp-servers/

# 5. System tools
sudo apt install pandoc ffmpeg

# 6. .env
cat > /opt/kesha-bot/.env << 'EOF'
TELEGRAM_BOT_TOKEN=<same>
ANTHROPIC_API_KEY=<same>
KESHA_NODE_ID=vps
KESHA_REDIS_URL=redis://:password@localhost:6379/0
DEEPGRAM_API_KEY=<key>
ALLOWED_USERS=720740564,893553748
WORK_DIR=/opt/cog-second-brain
CLAUDE_MODEL=claude-opus-4-6
KESHA_MCP_CONFIG=.mcp.json
# NO HTTPS_PROXY — VPS has direct API access
EOF

# 7. Systemd
sudo cp kesha-bot.service /etc/systemd/system/kesha-bot-vps.service
# Edit: WorkingDirectory, ExecStart, Environment
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
