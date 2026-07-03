# Migration Report — Kesha Bot: Timeweb → Contabo (#6)

**Date:** 2026-07-03
**Status:** ✅ **MIGRATION COMPLETE.** Bot LIVE on Contabo, live-tests passed, Timeweb decommissioned (stop+disable, data kept as rollback).

## Result
Bot migrated from Timeweb (72.56.235.40, Moscow, 2.9GB) to **Contabo (158.220.127.161, France, 8GB)**. Downtime ≈90s. **Zero data loss** — all DBs, RAG, session histories, reminders, secrets, MCP servers migrated and verified.

## Key architectural change: proxy dropped
Contabo is in France → direct reach to Anthropic/Telegram/Deepgram APIs (verified). Entire proxy stack removed: no `HTTPS_PROXY`/`HTTP_PROXY`/`TG_PROXY`/`NO_PROXY`, no Xray client. `bot.py:43` handles unset proxy natively (→ direct).

## What was done (by phase)
- **A** Contabo prep: users `kesha` (uid=1001, matching) + `tunnel`; installed node 20.20.2, npm, claude CLI 2.1.197, python venv/pip/rsync/git.
- **B** Staged via `sudo tar` on Timeweb → laptop (sha256 verified) → Contabo (re-verified): code, cog-vault, `~/.claude` (auth+projects 242M+MCP code), `.ssh`, Gmail OAuth. Captured uncommitted `claude_session.py` tweak (was already in working tree, arrived intact). Captured MCP `pip freeze` lists.
- **C** Rebuilt bot `.venv` (requirements.txt) + 4 MCP envs (yougile/mailru/gmail venvs via freeze, websearch `npm install`). All imports OK.
- **D** Stripped proxy from `.env` (+`KESHA_PRIORITY=primary`); grep confirmed 0 residual proxy config anywhere; new systemd unit `kesha-bot-vps` (no proxy); tunnel user `authorized_keys` + sshd `AllowTcpForwarding` verified; laptop→Contabo reverse-forward tested OK.
- **E** Claude auth: copied `.credentials.json` authenticated first try (`claude -p` → READY), no manual login needed. `import bot` loads all 5 MCP servers.
- **F CUTOVER:**
  - F1 stop old bot + pgrep gate → no poller left.
  - F2 `PRAGMA wal_checkpoint(TRUNCATE)` all 3 DBs: busy=0, quick_check=ok.
  - F3 final sync storage + session `.jsonl`; verified on Contabo: messages **677**, reminders **42**, RAG 1101 rowids/677 indexed; sessions `720740564`→45M, `893553748`→47M (memory preserved).
  - F4 bot `active (running)`, 142M RAM, clean logs, CWD=/opt/cog-second-brain.
  - F5 laptop tunnel re-point → **needs user sudo** (`sudo bash /tmp/repoint-tunnel.sh`), not passwordless.

## Data verified on Contabo
| Store | Value |
|-------|-------|
| messages.db | 677 messages |
| reminders.db | 42 reminders |
| vec.db (RAG) | 1101 fts/rowids, 677 indexed |
| session 720740564 | 9a27361a… .jsonl 45M |
| session 893553748 | 8896ca81… .jsonl 47M |

## Deviations from plan
- **B4 tweak patch:** `git apply` "failed" because the tweak was already present (tar captured working tree). No action needed — end state identical to Timeweb (git shows `M`, correct).
- **F5:** laptop sudo not passwordless → handed script to user instead of auto-applying (per SAFETY rules).
- **Phase G live tests** (bot answers / memory / RAG / reminders / MCP / voice) require the user to send Telegram messages — cannot be done by the agent. Server-side infra checks (no errors, single poller, RAM) passed.

## Rollback (still available)
Timeweb data untouched (only READ + checkpoint, no deletes). If Contabo fails: stop Contabo (pgrep gate) → start Timeweb → re-point tunnel. See plan.md ROLLBACK.

## Phase G — live verification (PASSED)
User confirmed via Telegram dialogue: bot answers + streaming, session memory/context preserved, MCP servers connected, self-diagnosed via Bash (hostname/free/curl → confirmed Contabo, 7.8G RAM). F5 tunnel re-pointed (`tunnel@158.220.127.161` active) → `run_on_laptop` live.

## Phase H — Timeweb decommission (DONE, no data deletion)
- Timeweb `kesha-bot-vps`: `is-active=inactive`, `is-enabled=disabled` (won't restart after reboot).
- pgrep gate: no `bot.py` process on Timeweb.
- Contabo: `is-active`, **NO Conflict** (single poller confirmed), no errors, up since 08:14:52 CEST.
- Timeweb data left INTACT as rollback: messages.db 1.15M, vec.db 5.0M, reminders.db, `~/.claude/projects/-opt-cog-second-brain` 242M.

## Docs updated
- Project `CLAUDE.md`: new IP `158.220.127.161`, no-proxy troubleshooting, Timeweb decommissioned note, Xray-don't-touch warning.
- `~/.claude/docs/vps-registry.md`: Contabo now hosts Kesha; Timeweb entry struck through.

## Migration complete ✅
Zero data loss. Downtime ≈90s. Proxy stack removed. Bot healthy on Contabo (8GB, France, direct API). Rollback still available on Timeweb if ever needed.
