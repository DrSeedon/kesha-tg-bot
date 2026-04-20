# Changelog

## v1.5.4 — 2026-04-20

### Fixed
- **Hanging draft at end of response** — in v1.5.3 `_finalize_text_block` only updated the draft with final Markdown text and relied on a subsequent sendMessage to auto-promote it. When the response ended on pure text (no tool status bubble to follow) the draft trigger `⠀` was sent+deleted too fast for TG to promote → user saw NO text at all, only the "🤖 Сделано" status bubble.
- Changed: keep SendMessageDraft for live streaming animation, but finalize by sending a **real `sendMessage`** with the full final text. This gives us a proper `message_id` to track, and the hanging draft is superseded by the real message on the client.

### Known tradeoff
A visible bubble may briefly flash during the transition as the draft is replaced by the real message. If TG client auto-promotes the draft rather than replacing it, we may see a brief dup — will monitor and iterate.

## v1.5.3 — 2026-04-20

### Changed
- **SendMessageDraft is back** — native Telegram streaming animation restored after the real dup root cause (v1.5.2) was fixed. With `has_deltas` guard in place, the draft → auto-promote pattern no longer double-delivers.
- Flow: `SendMessageDraft(draft_id, text, parse_mode=None)` during streaming → when text block ends, final `SendMessageDraft(..., parse_mode="Markdown")` with last full text → next `sendMessage` in chat (status bubble / next turn's draft / trailing trigger) auto-promotes the draft into a real permanent message.
- **End-of-response trigger**: if a turn ends and no subsequent sendMessage would naturally promote the draft (e.g. stream ended on text with no tool-status bubble after), a zero-width invisible message (`⠀` Braille blank) is sent and immediately deleted to force TG to finalize the hanging draft into a real message.

### Reasoning
editMessageText on a real message (v1.5.1/v1.5.2) worked but flickered. `SendMessageDraft` gives native, smooth character-by-character animation on the Telegram client — same UX as ChatGPT/Claude.ai streaming. With the dup bug fixed at the source (SDK `text` chunks vs `text_delta` chunks), draft's auto-promote behavior is now safe to rely on.

## v1.5.2 — 2026-04-20

### Fixed
- **Real root-cause of duplicate text** — SDK sends BOTH `text_delta` chunks (streaming) AND a final `text` chunk in `AssistantMessage` with the complete text. In v1.5.0 rewrite I dropped the `and not has_deltas` guard on the `text` branch, so both streams appended into `parts` → user saw the same text twice in one bubble. Restored `has_deltas` flag: `text_delta` sets it, `text` is only appended when flag is false. Reset flag on every `_finalize_text_block` for multi-turn responses.
- The previous v1.5.1 draft-removal was a red herring — dup was inside `parts` itself, not in TG delivery.

### Changed
- Tool checkmark is now green emoji `✅` instead of plain `✓`.

## v1.5.1 — 2026-04-20

### Fixed
- **Duplicate text bubbles** — after v1.5.0 if Claude streamed text and then called a tool, the user saw the same text TWICE: once as the auto-finalized draft (TG clients auto-promote a hanging `SendMessageDraft` into a real message when any other `sendMessage` arrives in the chat — including our status bubble), then again via our explicit `_send_safe`. Swapped `SendMessageDraft` + `_send_safe` for straightforward `message.answer` + `edit_message_text` on a real message. No more draft-finalize race, no dup.
- **Final stream bubble keeps its message_id** — when text block finalizes (tool/turn_done/end-of-stream), we now edit the existing streaming message with final Markdown-parsed text instead of sending a brand new message + leaving the streaming one as plain-text orphan.

## v1.5.0 — 2026-04-20

### Changed
- **Live tool status bubble** — all tool calls within one turn now live in a single persistent message with timers, instead of getting overwritten/lost. Shows `⏳ 🖥 Bash \`cmd\` · 12s` while running, `✓` when done. Stall marker `⏱` after 60s. Rate-limited edits (min 5s between) to stay under TG flood control.
- **Removed streaming/tool bubble conflict** — text streaming via `SendMessageDraft` and tool bubbles now live in separate messages. No more `edit_message_text` switching between tool-text and final-text in the same bubble.
- **Deleted dead code** — `_finalize_current_text` with its three-branch edit-in-place/delete-and-resend logic is gone. Now just: `_finalize_text_block` (send as new message) and `_finalize_status` (close out the live tool bubble). Removed `current_msg`, `current_is_tool`, `has_deltas`, `can_edit_in_place`.
- **Per-tool icons** — 🖥 Bash, 📖 Read, ✏️ Write/Edit, 🔎 Glob/Grep, 🌐 WebSearch/WebFetch, 🤖 Agent/Task, 📝 TodoWrite. Fallback `🔧` for unknown.

### Added
- `tool_status.py` with `ToolStatusTracker` — one message, live log of tool calls with running timers, handles rate-limits, flood control, and finalization on turn end.

### Reasoning
Previous UX: user sends a message → tool runs 2-5 min silently → `🔧 Bash ...` bubble gets overwritten each call → no history, no timer → feels hung. Now: full live log visible throughout, timer shows it's alive, all tools retained for context.

## v1.4.2 — 2026-04-18

### Fixed
- **False-positive stall on long tools** — previous `v1.4.1` used a single 120s chunk timeout, which killed legit long-running tool calls (Agent subtasks, deep websearch chains, big Bash operations that take 2–10 min and produce zero chunks from the SDK until they finish). Now two-tier:
  - `TEXT_STALL_TIMEOUT = 90s` — when the last chunk was `text_delta`/`text` (LLM actively writing, silence = real problem)
  - `TOOL_STALL_TIMEOUT = 600s` — when the last chunk was `tool` (tool in progress, SDK is silent by design)
  - Triggered case: Кеша launched an `Agent` subtask at 23:53 to generate FNS XML, subtask took >120s, loop aborted with "⚠️ ответ прервался" even though everything was fine.

## v1.4.1 — 2026-04-18

### Fixed
- **Stream stall / silent response loss** — if Claude SDK stopped producing chunks mid-stream (SSL drop on proxy, etc.), `_ask` hung forever and the user got no reply at all (the draft stayed frozen). Now each chunk is awaited with a 120s timeout; on stall:
  - Partial text is finalized with a `_(⚠️ ответ прервался — повтори если нужно)_` marker
  - Session is reconnected so the next message starts fresh
  - If nothing was ever finalized, user sees `⚠️ Ответ не пришёл (соединение прервалось). Повтори пожалуйста.` instead of silence
  - Triggered case: Катя asked about РКИ on 2026-04-18 23:35, bot streamed into draft, HTTPS proxy dropped, no `ResultMessage` arrived → loop hung, user asked "а где ответ ты че удалил"
- **Draft update dedup** — `_draft_update` now compares full text (not just length) against last sent, and silently swallows `message is not modified` errors instead of spamming DEBUG logs.

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
