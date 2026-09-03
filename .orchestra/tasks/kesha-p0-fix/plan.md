# Plan: P0+P1 Bug Fixes

## P0-1: CancelledError swallowed (response_stream.py)

**Problem:** Line 286 `except (Exception, asyncio.CancelledError)` catches CancelledError and retries instead of propagating. Two inner catches at lines 210 and 278 also swallow it.

**Fix (3 locations):**

1. **Line 286** — Split into two except blocks. Add `except asyncio.CancelledError: raise` BEFORE `except Exception as e:`.

2. **Line 210-211** — Inner catch during timeout retry cleanup. Currently logs and continues. Change to: `raise` (let the outer CancelledError handler propagate it).

3. **Line 278-279** — Inner catch during error retry cleanup. Same fix: `raise`.

**Files:** `response_stream.py` lines 210, 278, 286

## P0-2: _resolve_chat() unsafe fallback (kesha_tools.py)

**Problem:** `_resolve_chat()` falls back to `next(iter(ALLOWED))` when ContextVar empty. Chat-bound tools send to wrong user.

**Fix:**
1. Keep `_resolve_chat()` as-is (used by safe tools).
2. Add `_require_chat()` that returns chat_id or raises MCP error — no fallback.
3. Replace `_resolve_chat()` with `_require_chat()` in ALL chat-bound tools: send_photo, send_file, send_video, send_audio, send_voice, react, set_debounce, create_reminder, list_reminders, cancel_reminder, update_reminder.
4. Keep `_resolve_chat()` ONLY in: get_bot_status, toggle_debug (non-destructive).

**Files:** `kesha_tools.py` — add `_require_chat()` after line 35, change ~11 tool functions.

## P0-3: inject() not serialized (claude_session.py)

**Problem:** Concurrent `query()` calls on same ClaudeSDKClient = undefined behavior.

**Fix:**
1. Add `self._query_lock = asyncio.Lock()` in `__init__` (after line 53).
2. In `send_message()`: wrap `await self._client.query(text)` + `self._expected_results = 1` in `async with self._query_lock:` (lines 146-148). Do NOT hold lock during `receive_messages()` iteration.
3. In `inject()`: wrap `await self._client.query(text)` + `self._expected_results += 1` in `async with self._query_lock:` (lines 219-220).

**Files:** `claude_session.py` — __init__, send_message, inject

## P1-4: shutdown() incomplete (chat_state.py)

**Problem:** ChatRegistry.shutdown() cancels tasks but doesn't set _shutdown, doesn't await, doesn't close sessions.

**Fix:** Replace lines 620-626:
1. Set `chat._shutdown = True` for each chat.
2. Collect all tasks (debounce + processing) that are not done.
3. Cancel all tasks.
4. `await asyncio.gather(*tasks, return_exceptions=True)`.
5. Close all Claude sessions via `session._safe_disconnect()`.
6. Clear `_chats`.

**Files:** `chat_state.py` lines 620-626

## P1-5: Lazy reminders marked delivered too early (reminders.py)

**Problem:** `get_lazy_block_for_prompt()` marks delivered immediately. If _ask_fn fails, reminders lost.

**Fix:**
1. Rename function to return `(block, ids)` tuple instead of just block string.
2. Remove `db.mark_delivered_batch()` call from inside the function.
3. Move rescheduling logic out too — return `(block, ids, rows_to_reschedule)`.

Return `(block, ids, rows_to_reschedule)` — caller marks delivered AND reschedules after success.

3. In `get_lazy_block_for_prompt()`: remove `mark_delivered_batch()` call, remove reschedule loop. Instead collect rows that have repeat_interval or cycle_on_days. Return `(block, ids, reschedule_rows)`.
4. Add `mark_lazy_delivered(ids, rows_to_reschedule)` helper in reminders.py that does both mark_delivered_batch + reschedule.
5. Update caller in `chat_state.py` `_run_batch()` (line 411): capture `(block, lazy_ids, lazy_reschedule)`, call `mark_lazy_delivered(lazy_ids, lazy_reschedule)` after `_ask_fn` succeeds (after line 446).

**Files:** `reminders.py` lines 357-371, `chat_state.py` lines 411-414 + after line 446

## P1-6: inbox_server.py accepts any chat_id

**Problem:** No validation against ALLOWED set.

**Fix:** After line 29, cast chat_id to int (JSON may send string) and validate against ALLOWED:
```python
try:
    chat_id = int(chat_id)
except (TypeError, ValueError):
    return web.json_response({"error": "invalid chat_id"}, status=400)
from config import ALLOWED
if ALLOWED and chat_id not in ALLOWED:
    return web.json_response({"error": "chat_id not allowed"}, status=403)
```

**Files:** `inbox_server.py` — after line 29

## P1-7: Voice error loses file from LLM context (handlers.py)

**Problem:** When transcription fails, user sees error but voice file never reaches LLM. video_note has fallback, voice doesn't.

**Fix:** In h_voice, lines 269-274: instead of calling `transcription_finished(None)` and returning, create a fallback PendingEntry (like video_note does) and pass it to `transcription_finished()`. This avoids FSM stuck state from separate enqueue() call:
```python
if not text:
    err_msg = t(msg, "voice_fail")
    if err:
        err_msg += f" ({err})"
    await _send_safe(msg, err_msg)
    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[voice: {path}] (transcription failed: {err or 'unknown'})"
    fallback_entry = PendingEntry(
        prompt=full_prompt, message_id=msg.message_id, message=msg,
        source="user", reply_target=chat_id,
    )
    await cs.transcription_finished(fallback_entry, gen, media_gen)
    return
```

**Files:** `handlers.py` lines 269-275

## What NOT to touch
- No refactoring of surrounding code
- No changes to ChatState FSM logic
- No changes to tool_status.py, compact.py, media.py, telegram_io.py, config.py, bot.py
- No comments added to code
