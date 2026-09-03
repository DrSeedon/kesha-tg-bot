# Kesha TG Bot — Architecture Map (for Codex Debate)

## Stats
- 14 Python files, ~3840 LOC total
- All files in project root (flat, no package)
- 2 users, single VPS, no failover (removed in v2.1.0)

## Module Dependency Graph
```
bot.py (200 LOC) — bootstrap, wiring, main()
  ├── config.py (192) — env, logging, i18n STRINGS, t()
  ├── chat_state.py (626) — ChatPhase FSM, PendingEntry, ChatState, ChatRegistry
  │     └── claude_session.py (288) — ClaudeSDKClient wrapper
  ├── handlers.py (518) — all @dp.message handlers, set_commands()
  │     ├── config.py
  │     ├── media.py (214) — download_file, transcribe (Deepgram), cleanup
  │     ├── telegram_io.py (160) — user_prefix, send_safe, split_msg, typing_loop
  │     └── chat_state.py (PendingEntry)
  ├── response_stream.py (317) — _ask() streaming, drafts, retries
  │     ├── telegram_io.py
  │     └── tool_status.py (225) — ToolStatusTracker (live tool bubble)
  ├── kesha_tools.py (365) — MCP tools (14 tools: media, reminders, config, react)
  │     └── reminders.py (448) — SQLite DB, scheduler loop, repeat/cycle, lazy TTL
  ├── compact.py (152) — context compaction (summarize → reset → continue)
  └── inbox_server.py (70) — HTTP inbox from Orchestra
```

## Data Flow
```
TG Message → handlers.py → PendingEntry → ChatState.accept_entry()
  ↓ debounce (3s) or inject (if PROCESSING)
ChatState._run_batch() → response_stream._ask()
  ↓ streaming
Claude SDK → text_delta/tool/result/turn_done/error chunks
  ↓
Telegram: SendMessageDraft (streaming) + sendMessage (final)
         + ToolStatusTracker (live tool bubble)
  ↓ after response
auto-compact check → drain deferred → IDLE
```

## ChatState FSM
```
IDLE → COLLECTING (debounce armed)
     → WAITING_MEDIA (transcription pending)
     → PROCESSING (Claude turn active)
     → STOPPING (interrupt requested)
     → COMPACTING (context compaction)
     → back to IDLE
```

## Key Patterns
1. **Late binding via set_bot()** — modules receive bot/registry at runtime, avoids circular imports
2. **contextvars for chat routing** — `_current_chat_id` ContextVar for MCP tool routing
3. **Persistent sessions** — session_id saved to file, survives restart
4. **Message injection** — inject() during PROCESSING, _expected_results counter
5. **Debounce + batch** — rapid messages collected, sent as one prompt
6. **Transcription pipeline** — voice/video_note → Deepgram → PendingEntry with text

## Coupling Analysis

### High coupling (concerning)
1. **bot.py → everything**: wires all modules, passes 11 callback params to ChatRegistry
2. **ChatState constructor**: 11 params including 5 function callbacks (ask_fn, compact_session_fn, etc.)
3. **kesha_tools.py → bot module**: `_bot_ref` gives access to bot, registry, session — tight coupling
4. **handlers.py global state**: `_bot`, `_registry`, `_uptime_fn` module-level globals set via setters

### Low coupling (good)
1. **tool_status.py** — self-contained, clean interface
2. **compact.py** — pure functions, takes session + notify callback
3. **media.py** — standalone, only needs bot for download
4. **telegram_io.py** — pure utilities, only needs bot for typing_loop
5. **reminders.py** — mostly standalone, clean DB layer

## Potential Issues for Debate

### Structure
- All files in root — not a Python package, no __init__.py
- `from chat_state import PendingEntry` works only because root is in sys.path
- No test suite at all

### God Object tendencies
- ChatState: 626 LOC, manages FSM + batching + processing + compaction + inject + drain
- handlers.py: 518 LOC, 20+ handler functions, media handling duplicated across voice/video_note

### Concurrency
- asyncio.Lock per ChatState — good
- But _run_batch runs OUTSIDE lock for most of its body
- inject() checks _is_processing without lock
- No cancellation handling in _run_batch except basic CancelledError

### Error handling
- response_stream.py: complex retry logic (retries + need_retry flag + inner/outer loops)
- compact.py: good error handling, proper ok/error distinction
- reminders.py: fire-and-forget create_task for urgent_llm delivery

### Dead/unused code
- setup_wizard.py (64 LOC) — not imported anywhere
- `_shutdown` field in ChatState — always False (leftover from failover removal)

### Duplication
- extract_text_with_urls / extract_caption_with_urls — near-identical functions
- send_photo/send_file/send_video/send_audio/send_voice — 5 nearly identical MCP tools
- h_voice / h_video_note — transcription logic duplicated

### Security
- Path traversal mitigated in download_file (Path(name).name)
- No input validation on inbox_server (no auth, localhost only)
- systemctl restart via subprocess — safe (ALLOWED check upstream)
