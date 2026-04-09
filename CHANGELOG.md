# Changelog

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
