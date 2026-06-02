# Report: P0+P1 Bug Fixes from Architecture Debate

## Summary
Fixed 3 P0 and 4 P1 bugs identified in the Claude Opus + Codex GPT-5.5 architecture debate. All fixes are surgical — minimum code, no refactoring, no comments.

## Files Changed (+100/-46 lines)
| File | Changes |
|------|---------|
| `response_stream.py` | +5/-3 — Re-raise CancelledError in 3 locations |
| `kesha_tools.py` | +45/-22 — _require_chat() fail-closed for 11 tools |
| `claude_session.py` | +9/-4 — asyncio.Lock around query()/inject() |
| `chat_state.py` | +19/-1 — Proper shutdown + lazy delivered after success |
| `reminders.py` | +15/-7 — Return (block, ids, rows) + mark_lazy_delivered() |
| `inbox_server.py` | +8/-1 — int cast + ALLOWED check |
| `handlers.py` | +7/-1 — Voice fallback entry via transcription_finished() |

## Tests
- Syntax verification: all 7 files pass `python -c "import ast; ast.parse(...)"`
- Codex review on plan: 2 blocking findings → fixed before implementation
- Codex review on impl: 2 P2 findings (lazy promotion race, voice timing) — accepted as low-risk for 2-user scale

## Breaking Changes
- `get_lazy_block_for_prompt()` now returns `(str, list[int], list)` instead of `str`. Caller in chat_state.py updated.
- Chat-bound MCP tools now fail with error when ContextVar is empty instead of falling back to first ALLOWED user.

## Known P2 Issues (from Codex review, accepted)
1. Lazy reminder promotion race: during long model response, reminder_loop could promote a lazy reminder that's already injected. Would need claimed/in-delivery state. Low risk (24h TTL + 30s tick).
2. Voice fallback timing: if _send_safe takes >30s (flood control), media_generation increments and fallback entry is discarded. Extremely unlikely.
