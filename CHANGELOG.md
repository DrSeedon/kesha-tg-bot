# Changelog

## v1.4.0 — 2026-04-14

### Added
- **Multi-user support** — each user gets their own isolated `ClaudeSession` with separate session files (`storage/sessions/<chat_id>`). No more cross-chat message leaking or response mixing. Sessions created lazily on first message.
- **Unknown user response** — unauthorized users get their Telegram ID on first message (once per session), so the owner can easily add them to `ALLOWED_USERS`.
- **Supplements dashboard fixes** — blocked "+" button when inventory is zero, low-stock warning (≤2 days), sorted log by date to fix phantom stock calculation.

### Fixed
- **Cross-chat response mixing** — responses no longer leak between users. Each chat has its own Claude CLI process and streaming pipeline.
- **Phantom inventory in supplements dashboard** — unsorted log caused `max(0, 0-dose) = 0` to silently eat entries. Now sorted before calculation both in data and server code.

### Changed
- `claude_session.py` — `session_file` parameter per instance instead of global `SESSION_FILE`. Migration from old `storage/session_id` supported.
- `reminders.py` — supports callable `get_session(chat_id)` for per-chat inject/processing check.
- Removed `_global_lock` and `_queued_batches` — no longer needed with per-user sessions.

## v1.3.0 — 2026-04-14

### Added
- **msg_id on every message** — single messages now include `[msg_id=X]` tag in prompts, enabling accurate emoji reactions on any message (not just batches).
- **LLM greeting on MCP restart** — when bot is restarted via `restart_bot` MCP tool, Claude writes an in-character greeting instead of static text. Uses file-flag (`storage/greet_on_restart`) with fsync to survive process kill. Normal restarts (systemd/crash) still show plain "Кеша запущен!".
- **Retry with backoff for urgent_llm** — handler retries 3x (15/30/45s delays) on network errors. Fallback to raw text also retries 3x.

### Fixed
- **restart_bot MCP tool** — no longer fails with empty error. Tool now returns immediately ("Bot restarting in 1s...") and schedules the actual `systemctl restart` 1s later via `call_later`, avoiding the race condition where the process kills itself before `communicate()` returns.
- **Emoji reactions on wrong messages** — reactions no longer land on bot's own messages when msg_id is unknown.

### Changed
- `README.md` — added reminders & reactions features documentation (EN + RU), bumped to v1.3.0.

## v1.2.0 — 2026-04-13

### Added
- **Reminders system** (`reminders.py`) — SQLite-backed persistent reminders with 3 types:
  - `plain` — bot sends raw text at the time, no LLM
  - `urgent_llm` — at the time, Claude is triggered (via inject if busy, new turn if idle) to formulate and send the reminder
  - `lazy_llm` — silent at fire time; injected into the next user prompt as context
- **Universal repeat**: `repeat_interval` (`30m`/`2h`/`1d`/`1w`/`3mo`) + optional `repeat_at_time` (`HH:MM`) for daily/weekly alignment.
- **Lazy TTL**: `lazy_llm` reminders not delivered within 24h auto-promote to `urgent_llm`.
- **Missed delivery on startup**: groups missed reminders by type and dispatches accordingly (plain → digest, urgent_llm → Claude turn, lazy_llm → mark fired for next user message).
- **MCP tools**: `create_reminder`, `list_reminders`, `cancel_reminder`, `update_reminder`.
- **Time prefix in prompts**: every prompt now starts with `[YYYY-MM-DD HH:MM +0700]` so Claude has accurate current time in user's timezone (Krsk UTC+7).

### Removed
- `schedule_message` MCP tool — replaced by `create_reminder` (persistent across restarts, supports repeat/cancel/update).

### Changed
- `system_prompt.txt` — added TIME & TIMEZONE and REMINDERS sections explaining the 3 types and how to interpret fired reminder blocks.
- `requirements.txt` — added `python-dateutil` for `relativedelta` (correct month arithmetic).

## v1.1.0 — 2026-04-09

### Fixed
- **Stale response buffer** — injection messages no longer leave orphaned responses in the SDK buffer. Switched from `receive_response()` (stops at first ResultMessage) to `receive_messages()` with manual ResultMessage counting. Each `query()` and `inject()` increments expected results counter; the loop breaks only when all results are consumed.
- **Injection responses merged into single bubble** — each Claude turn (main response + injection responses) now finalizes as a separate Telegram message via `turn_done` signal.

### Changed
- `claude_session.py` — `receive_messages()` + `_expected_results` counter instead of `receive_response()`. Added `_is_processing` flag to prevent injection after response completes.
- `bot.py` — handle `turn_done` chunk type to finalize text between turns.

### How injection works now
1. User sends message → `query()` → `_expected_results = 1`
2. User sends follow-up while Claude is thinking → `inject()` → `_expected_results += 1`
3. `receive_messages()` streams all responses; each `ResultMessage` decrements counter
4. When counter hits 0 → break (all responses consumed, no stale buffer)
5. Each intermediate `ResultMessage` triggers `turn_done` → text finalized as separate TG message

## v1.0.0 — 2026-04-08

### Initial release
- Telegram bot on Claude Agent SDK (ClaudeSDKClient, persistent connection)
- All media types: photo, voice, video, document, audio, sticker, video notes, albums
- Native streaming via SendMessageDraft (Bot API 9.5)
- Message injection while Claude is thinking
- Native interrupt via `/stop`
- Debounce + batching of rapid messages
- Smart tool/text display (tools in ephemeral bubbles)
- Persistent session surviving restarts
- Media cache (file_unique_id, persistent JSON)
- Deepgram Nova-2 STT for voice/video notes
- i18n (RU/EN)
- MCP tools: send_photo, send_file, send_video, send_audio, send_voice, schedule_message, self-config
- Live model switching, context usage tracking
- Auto-retry on session errors
- Global MCP server loading (~/.claude.json, settings.json, .mcp.json)
- Setup wizard for first-run configuration
