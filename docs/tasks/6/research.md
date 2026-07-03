# Research — Migration Kesha Bot: Timeweb → Contabo (#6)

**Date:** 2026-07-03
**Goal:** Full clone of Kesha TG bot from Timeweb (72.56.235.40, 2.9GB RAM, RF/Moscow) to Contabo (158.220.127.161, 8GB RAM, France). Zero data loss. Bot works on new host, old one stopped (not deleted).

---

## 🥇 PRIORITY #1 RESOLVED: No proxy needed on Contabo

**Contabo is in France (Lauterbourg, Grand Est), AS51167 Contabo GmbH.** NOT in Russia → no ТСПУ/РКН filtering of outbound traffic. Direct connectivity test from Contabo:

| Endpoint | Result | Meaning |
|----------|--------|---------|
| `api.anthropic.com` | HTTP 401, connect 14ms | ✅ reached (401 = no auth header, expected) |
| `api.telegram.org` | HTTP 302, connect 14ms | ✅ reached (redirect) |
| `api.deepgram.com` | HTTP 404, 48ms | ✅ reached (no path) |

**Conclusion: DROP the entire proxy stack on Contabo.**
- No `HTTPS_PROXY` / `HTTP_PROXY` / `TG_PROXY` env vars
- No `NO_PROXY`
- No Xray/Ёжик client for outbound (existing Xray on Contabo is Maxim's *inbound* VLESS server — DO NOT TOUCH)
- Code handles this natively: `bot.py:43` → `_tg_proxy = os.getenv("TG_PROXY") or os.getenv("HTTPS_PROXY") or None` → when unset, `None` → aiogram direct. Claude SDK reads `HTTPS_PROXY` from env → unset → direct.

This massively simplifies the systemd unit.

---

## Current architecture (Timeweb source)

```
/opt/kesha-bot          — code + .venv + .env + storage/   (git: DrSeedon/kesha-tg-bot.git)
/opt/cog-second-brain   — bot CWD (WORK_DIR), COG vault      (git: DrSeedon/COG-second-brain.git, PRIVATE)
/home/kesha/.claude/    — Claude CLI config, auth, MCP servers, session histories
```

**Users:** `deploy` (SSH login), `kesha` (bot runs as this user, `HOME=/home/kesha`).
**systemd:** `kesha-bot-vps.service`, `User=kesha`, `ExecStart=/opt/kesha-bot/.venv/bin/python3 bot.py`.

---

## What must migrate (complete inventory)

### 1. storage/ — CRITICAL DATA (119M total)
Path: `/opt/kesha-bot/storage/`
| Item | Size | Notes |
|------|------|-------|
| `messages.db` (+`-wal` 4.1M, +`-shm` 32K) | ~5M | full msg log (user+assistant) |
| `vec.db` (+`-wal` 4.2M, +`-shm` 32K) | ~9M | RAG embeddings (sqlite-vec e5-small) |
| `reminders.db` (+`-shm`) | 37K | reminders |
| `sessions/720740564`, `sessions/893553748` | 36B each | **UUID pointers** into Claude CLI history (see §4) |
| `media/` | 104M / 308 files | cache — regenerable, but small → copy anyway |

⚠️ **ALL .db in WAL mode.** Recent writes live in `-wal`. Must **stop bot first** (quiesces writers) then checkpoint OR copy all 3 files (.db+.db-wal+.db-shm) together. `sqlite3` CLI NOT installed — use Python `PRAGMA wal_checkpoint(TRUNCATE)` (sqlite 3.45.1 on both).

### 2. Code — /opt/kesha-bot
- git remote: `git@github.com-kesha:DrSeedon/kesha-tg-bot.git` (SSH host alias). Clean except:
- ⚠️ **Uncommitted local prod tweak** in `claude_session.py` (NOT in git):
  ```python
  +thinking={"type": "adaptive"},
  +effort="high",
  ```
  (options block ~line 93). MUST preserve — rsync this file or re-apply after clone.
- `.venv` — recreate via `requirements.txt` (aiogram, claude-agent-sdk, sqlite-vec, fastembed, etc.). Python 3.12.3 on both → clean.
- `.env` — SECRETS (see §5). Copy, never commit.
- Other loose files present in repo but gitignored/local: `banner.png`, `system_prompt.txt`, `logs/`, `artifacts/`, `docs/`. `system_prompt.txt` is loaded at runtime → MUST copy (check if in git; if gitignored → rsync).

### 3. CWD — /opt/cog-second-brain (64M)
- git: `git@github.com-cog:DrSeedon/COG-second-brain.git` (PRIVATE). Status **clean**, 0 unpushed commits. Personal notes (00-inbox..05-knowledge) are git-tracked.
- Could `git clone`, but to be 100% safe against untracked/ignored files (`.obsidian/`, `.trash/`) → rsync whole dir.
- Contains `.mcp.json` (MCP server config — see §6).

### 4. Claude CLI state — /home/kesha/.claude/ (session histories = MOST CRITICAL, easy to miss)
- **`~/.claude/projects/-opt-cog-second-brain/` = 242M** — the ACTUAL conversation histories as `.jsonl`.
  - Active sessions: `9a27361a-…jsonl` (46M) ↔ `sessions/720740564`, `8896ca81-…jsonl` (48M) ↔ `sessions/893553748`.
  - The `storage/sessions/<chat_id>` files just contain the UUID. **If projects/ not migrated → sessions resume empty (memory loss).**
  - ⚠️ Dir name encodes the CWD path (`-opt-cog-second-brain`). If CWD stays `/opt/cog-second-brain` on Contabo → same dir name → works. **Keep CWD path identical.**
- **`~/.claude/.credentials.json`** — OAuth token (`sk-ant-oat01-…` + refreshToken, expiresAt=access-token short expiry, auto-refreshes). Account-bound, NOT host-bound → copying *should* work; refreshToken renews access. Fallback: manual `claude auth login` (needs browser, no proxy on Contabo).
- `~/.claude.json` — global config: MCP approvals, project trust, `hasCompletedOnboarding`. Host/path-specific but paths match (same /opt/cog-second-brain). Copy to skip re-onboarding/trust prompts.
- `~/.claude/settings.json`, `~/.claude/CLAUDE.md` — global agent config. Copy.
- `~/.claude/skills/`, `plugins/`, `mcp-configs/`, `session-env/` — copy (behavior parity).
- `debug/`, `telemetry/`, `cache/`, `backups/` — skip (regenerable noise).

### 5. Secrets — /opt/kesha-bot/.env
```
TELEGRAM_BOT_TOKEN=8214845152:AAH…          # bot identity
ALLOWED_USERS=720740564,893553748
CLAUDE_MODEL=claude-opus-4-6
WORK_DIR=/opt/cog-second-brain              # keep identical (session dir name!)
DEEPGRAM_API_KEY=630a841ff…                 # voice transcription
DEBUG=true  DEBOUNCE_SEC=3  AUTO_COMPACT_PCT=95  MEDIA_MAX_MB=100
MEDIA_DIR=./storage/media  LOG_DIR=./logs
HTTPS_PROXY / HTTP_PROXY / TG_PROXY / NO_PROXY  # ← REMOVE these on Contabo
KESHA_PRIORITY=secondary                     # ← set to primary/remove (failover concept dead)
```

### 6. MCP servers — /home/kesha/.claude/mcp-servers/ (464M)
Config: `/opt/cog-second-brain/.mcp.json`. 4 servers:
| Server | Runtime | Secrets / OAuth |
|--------|---------|-----------------|
| `yougile` | python venv 3.12.3 | creds inline in .mcp.json (email/pw/api_key) |
| `mailru` | python venv 3.12.3 | (none in config) |
| `gmail` | python venv (`workspace-mcp`) | OAuth: `gmail-mcp/credentials/token.pickle` + `client_secret.json`; also `~/.google_workspace_mcp/credentials/maxim2000as@gmail.com.json` — **MUST copy or Gmail needs browser re-auth** |
| `websearch` | node (`index.js`) | `OPENROUTER_API_KEY` inline in .mcp.json; needs `node_modules` |

- venvs 3.12.3 = Contabo's python → **recreate venvs** (`python3 -m venv` + pip install) is cleaner than copying binaries. Need each server's requirements (check `requirements.txt`/`pyproject` per dir).
- websearch: `npm install` in its dir (needs node).
- **Gmail OAuth tokens (pickle/json) MUST be copied verbatim** — cannot regenerate headless.

### 7. Reverse SSH tunnel — laptop ↔ VPS (for `run_on_laptop` MCP tool)
Two directions:
- **Laptop→VPS** (`ssh-tunnel-vps.service` on laptop, `User=maxim`): `ssh -N -R 127.0.0.1:2222:localhost:22 … -i ~/.ssh/tunnel_vps tunnel@72.56.235.40`. Exposes laptop SSH on VPS:2222. → **update IP to 158.220.127.161**.
- **VPS→laptop** (`run_on_laptop` in `kesha_tools.py:408`): `ssh -p 2222 -i /home/kesha/.ssh/tunnel_laptop maxim@localhost`. Uses tunnel back to laptop.
- On Contabo need: user `tunnel` (nologin, restricted) with authorized_keys = laptop's `~/.ssh/tunnel_vps.pub` (`restrict,port-forwarding,permitlisten="127.0.0.1:2222"`). Copy `/home/kesha/.ssh/tunnel_laptop` (+config, known_hosts) key so bot can dial back.
- Laptop's `~/.ssh/authorized_keys` ALREADY contains kesha's `tunnel_laptop.pub` (verified) → `run_on_laptop` works once tunnel up. No laptop-side key change needed beyond IP.

### 8. Claude CLI + node install on Contabo
- Contabo has: python3 3.12.3, git. **MISSING: node, npm, claude CLI, uv.**
- Old VPS: node v20.20.2, claude = npm global `@anthropic-ai/claude-code` (`/usr/bin/claude` → `lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`).
- Install on Contabo: nodejs 20 (nodesource), `npm i -g @anthropic-ai/claude-code`, python venv (system python fine). `uv` optional (not used by bot; requirements.txt via pip).

---

## Contabo target state (verified)
- Ubuntu 24.04.4, kernel 6.8, 8GB RAM (7.1G free), 4 CPU, 142G free disk. Swap 0 (fine at 8GB).
- Ports in use: 22(SSH), 443/8443(Xray VLESS — Maxim's, DO NOT TOUCH), 18080(tinyproxy localhost), 4443(MTProto). Bot needs NO listening ports (outbound only + reverse tunnel :2222 which is fine).
- `kesha` user: does NOT exist → create. `tunnel` user: does NOT exist → create.
- UFW off.

---

## Risks & edge cases

1. **WAL data loss** — mitigated by stop-bot-first + checkpoint. If bot writes during copy → lose recent msgs/RAG. Order: stop → checkpoint → verify → copy.
2. **Session history not migrated** — biggest silent-loss risk. `~/.claude/projects/-opt-cog-second-brain/` MUST go. Keep CWD path identical so dir name matches.
3. **Claude auth** — copy `.credentials.json`; if refresh fails on new host, fallback `claude auth login`. Contabo direct → no proxy needed for login.
4. **Gmail OAuth** — headless re-auth impossible. Copy `token.pickle` + workspace creds. Verify Gmail MCP loads after.
5. **Uncommitted `claude_session.py` tweak** (`thinking`/`effort`) — preserve.
6. **MCP venv recreation** — need each server's deps. If a server has no requirements.txt, `pip freeze` its venv on old host first.
7. **`system_prompt.txt`** — runtime-loaded, may be gitignored → must copy explicitly.
8. **fastembed model download** — first RAG use downloads e5-small (~118M) ONNX. Contabo direct internet → fine, but first startup slower. RAM 8GB → no OOM risk (old OOM was 2.9GB).
9. **Timezone** — Contabo = Europe/Paris, old = Moscow. Reminders store absolute times? Check `reminders.py` tz handling → may need to set server TZ or verify reminders use UTC/user-tz. **Investigate in plan.**
10. **`.claude.json` path-trust** — if not copied, first `claude` run may prompt trust/onboarding, blocking headless bot start. Copy it.
11. **git SSH host aliases** (`github.com-kesha`, `github.com-cog`) — defined in kesha's `~/.ssh/config`. If using git clone on Contabo, must copy that ssh config + deploy keys. Since rsyncing code dirs instead → clone not needed, but future `git pull` (deploy workflow) needs the SSH config + keys migrated → copy `/home/kesha/.ssh/` wholesale (id_ed25519, id_ed25519_cog, config, tunnel_laptop).

---

## Migration data map (what → how)

| Source | Method | Reason |
|--------|--------|--------|
| `/opt/kesha-bot/storage/*.db*` | stop→checkpoint→rsync | WAL safety |
| `/opt/kesha-bot/storage/{sessions,media}` | rsync | data + cache |
| `/opt/kesha-bot` code | rsync (excl .venv,__pycache__) then recreate .venv | preserve local tweak + gitignored files |
| `/opt/kesha-bot/.env` | rsync then EDIT (drop proxy) | secrets |
| `/opt/cog-second-brain` | rsync (excl .git optional) | vault + notes |
| `/home/kesha/.claude/projects/` | rsync | session histories (242M) |
| `~/.claude/.credentials.json`, `.claude.json`, `settings.json`, `CLAUDE.md`, `skills/`, `plugins/`, `mcp-configs/`, `session-env/` | rsync | auth + config |
| `~/.claude/mcp-servers/` (code, not venv) | rsync code + recreate venvs/node_modules | 4 MCP servers |
| gmail OAuth (`token.pickle`, workspace creds) | rsync | headless re-auth impossible |
| `/home/kesha/.ssh/` | rsync | git aliases + tunnel key |
| systemd unit | rewrite on Contabo (drop proxy) | simplified |
| laptop `ssh-tunnel-vps.service` | edit IP → 158.220.127.161 | reverse tunnel |

**Transfer route:** files are `0600`/`0700` under kesha → need sudo/root to read. Options: (a) rsync via `root@contabo` pulling from `deploy@timeweb` won't work for root-owned reads. Cleanest: on Timeweb `sudo tar` the kesha-owned paths → scp tarball → extract on Contabo as kesha. OR run rsync as root on both ends (root@contabo ← root@timeweb) if root SSH allowed on Timeweb (only `deploy` login exists → use `sudo rsync` with deploy + become). **Decide transfer mechanism in plan** (likely: staging tarball via sudo on Timeweb → scp → extract on Contabo).

---

## Resolved during research

- **Reminders timezone = SAFE, no action.** `reminders.py`: all times stored/compared as UTC (`utc_now()`, `astimezone(timezone.utc)`). Display hardcoded `KRSK_TZ = UTC+7`. Server local tz never used → Contabo Paris tz irrelevant.
- **MCP deps source:** `yougile-mcp/requirements.txt` exists. `mailru-mcp` — no requirements file (pip freeze its venv as source). `gmail-mcp` = pip package `workspace-mcp==1.20.3` (`pip install workspace-mcp==1.20.3`). Strategy: `pip freeze` each venv on old host → use as requirements to recreate on Contabo.

## Open questions for plan phase
- Transfer mechanism (tarball vs direct rsync, given kesha files are 0600/0700 → need root/sudo). **Leaning: sudo tar on Timeweb → scp → extract as kesha on Contabo.**
- Whether to `git clone` code fresh + re-apply tweak, or rsync (rsync chosen — preserves local tweak + gitignored `system_prompt.txt`).
- Downtime window ordering (stop old bot right before final DB sync).
