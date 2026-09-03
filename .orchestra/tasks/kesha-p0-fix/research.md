# Research: P0+P1 Bug Fixes from Architecture Debate

## Bug Verification

### P0-1: CancelledError swallowed in response_stream.py:286
**CONFIRMED.** Line 286: `except (Exception, asyncio.CancelledError) as e:` — catches CancelledError and treats it as a retriable error (retries += 1, reconnect). This prevents /stop and shutdown from working — they become retry loops instead.

Also confirmed: lines 210-211 and 278-279 have inner `except asyncio.CancelledError` blocks that log but **continue** (don't re-raise). These are inside retry cleanup paths and silently swallow cancellation during reconnect flow.

### P0-2: _resolve_chat() falls back to wrong chat in kesha_tools.py:34
**CONFIRMED.** Line 35: `return get_current_chat() or (next(iter(_bot_ref.ALLOWED), None) if _bot_ref else None)` — when ContextVar is empty (e.g. during reminder processing or race conditions), falls back to first ALLOWED user. Chat-bound tools (send_photo, send_file, react, etc.) use this and can send media/reactions to wrong user.

Safe tools that should keep fallback: toggle_debug, get_bot_status (non-destructive, non-chat-bound).
Unsafe tools that MUST fail-closed: send_photo, send_file, send_video, send_audio, send_voice, react, set_debounce, create_reminder, list_reminders, cancel_reminder, update_reminder.

### P0-3: inject() not serialized in claude_session.py:215
**CONFIRMED.** `inject()` at line 215 calls `await self._client.query(text)` with no lock. ChatState releases its lock before calling inject (line 125 in chat_state.py — inject call is outside `async with self._lock`). Two quick messages during PROCESSING = two concurrent query() calls on same ClaudeSDKClient = undefined behavior.

### P1-4: shutdown() incomplete in chat_state.py:620
**CONFIRMED.** Lines 620-626: cancels tasks but doesn't set `_shutdown=True`, doesn't `await` the cancelled tasks, doesn't close Claude sessions. `_shutdown` flag exists on ChatState (line 91) but `shutdown()` on ChatRegistry never sets it.

### P1-5: Lazy reminders marked delivered before _ask_fn succeeds (reminders.py:357)
**CONFIRMED.** `get_lazy_block_for_prompt()` at line 367 calls `db.mark_delivered_batch()` immediately after fetching. If _ask_fn fails later, reminders are lost — marked delivered but never seen by user.

### P1-6: inbox_server.py accepts any chat_id (line 29)
**CONFIRMED.** Line 29: `chat_id = data.get("chat_id", NOTIFY_CHAT)` — no validation against ALLOWED. Anyone who can POST to localhost:18081 can inject messages to any chat_id.

### P1-7: Voice transcription error loses file from LLM context (handlers.py:270)
**CONFIRMED.** Lines 269-274: when transcription fails (`not text`), user gets error message but no entry is enqueued to LLM. Compare to video_note (lines 370-378) which has a fallback entry `[video_note: {path}]`. Voice handler just returns after error.

## Files Affected
- `response_stream.py` — lines 210, 278, 286
- `kesha_tools.py` — line 34-35, plus each tool that calls _resolve_chat()
- `claude_session.py` — line 215-224 (inject method)
- `chat_state.py` — lines 620-626 (shutdown)
- `reminders.py` — lines 357-371 (get_lazy_block_for_prompt)
- `inbox_server.py` — line 29
- `handlers.py` — lines 269-274 (h_voice)

## Risks
- P0-1: Fix must re-raise CancelledError BEFORE the general except block, otherwise retry loop continues
- P0-3: Lock must NOT be held during streaming (receive_messages iterator) — only around query() calls
- P1-5: Must return IDs without marking, let caller mark after success
