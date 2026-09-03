## Summary

The plan targets the right bugs, but two proposed fixes need revision before implementation. One can leave the chat state stuck, and one can break repeating lazy reminders.

## Findings (blocking/suggestion/nit)

blocking: `docs/tasks/kesha-p0-fix/plan.md:89-95` proposes `transcription_finished(None, ...)` and then `enqueue(...)` for failed voice transcription. In `chat_state.py:170-190`, if debounce already moved the chat to `WAITING_MEDIA`, `transcription_finished(None, ...)` can set phase to `PROCESSING` with an empty batch and start no processing task. The later `enqueue()` then sees `PROCESSING`, tries `session.inject()`, likely fails because no Claude turn is active, and requeues into `deferred` with nothing to drain it. Fix by creating a fallback `PendingEntry` and passing it directly to `cs.transcription_finished(fallback_entry, gen, media_gen)`, like `h_video_note`.

blocking: `docs/tasks/kesha-p0-fix/plan.md:59-65` says to simplify lazy reminders to `(block, ids)`, but current repeat/cycle lazy rescheduling happens in `reminders.py:368-370`. If implementation removes that logic and only marks IDs delivered after `_ask_fn`, repeated/cycle lazy reminders stay fired/delivered and stop recurring. Return enough data to reschedule after successful `_ask_fn`, for example `(block, ids, rows_to_reschedule)`, or refetch rows by ID before/after marking delivered.

suggestion: `docs/tasks/kesha-p0-fix/plan.md:73-77` should cast and validate `chat_id` before checking `ALLOWED`. JSON can provide `"123"` as a string or an invalid type. Use `chat_id = int(data.get("chat_id", NOTIFY_CHAT))` inside a `try`, then reject invalid values with 400 and disallowed IDs with 403.

suggestion: `docs/tasks/kesha-p0-fix/plan.md:25` leaves `get_bot_status` on `_resolve_chat()` fallback. It is not destructive, but it is still per-chat and can expose the wrong session/status when context is missing. Safer: make `get_bot_status` require chat too, or split global bot status from per-chat session status.

## Verdict

Revise before implementation. The main bug list is valid, but P1-7 and P1-5 need concrete plan changes to avoid a stuck chat state and lost repeating lazy reminders.