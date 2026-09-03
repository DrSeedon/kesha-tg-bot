# Task #32 report

## Result

T1 is implemented in the fallback ingress. An allowed parsed aiogram 3.30.0 rich message now enters the existing pipeline exactly once as:

    [rich_message] <msg.rich_message.model_dump_json(exclude_none=True)>

An allowed fallback message with text/caption keeps the original text. An allowed empty fallback now enters the same pipeline once with [unhandled message: <content_type>]. No ChatState, media, debounce, dispatcher-order, or message-log code changed.

## Files

- handlers.py:8 — imported ContentType.
- handlers.py:656-668 — serialized ContentType.RICH_MESSAGE, preserved ordinary text/caption fallback, added explicit empty marker, and made the single enqueue call unconditional for allowed fallback updates.
- tests/test_rich_message.py was frozen at RED commit 4e6ee65 and remained byte-identical; it was not changed during implementation.

## Ticket

- T1 complete: valid rich paragraph, empty rich blocks, unknown-with-text compatibility, unknown-empty explicit admission, and exactly-once enqueue are covered by tests/test_rich_message.py::test_t1_fallback_handles_rich_and_unknown_messages.

## Verification

Focused acceptance:

    /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_rich_message.py tests/test_activity_ingress.py
    19 passed in 8.84s

The focused test alone:

    /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_rich_message.py::test_t1_fallback_handles_rich_and_unknown_messages
    4 passed in 8.40s

Full suite probe:

    /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -x -q
    410 passed, 1 skipped; stopped at tests/test_rag.py::test_index_and_search_semantic
    ModuleNotFoundError: No module named 'onnxruntime'

The full-suite failure is an environment dependency gap in the pre-existing RAG test and is outside this ticket. No uv.lock was modified.

## Reversible mutation checks

Each mutation was applied only to handlers.py, run against the frozen oracle, then restored. The oracle stayed byte-identical.

1. Disabled the rich branch: 2 failed, 2 passed; both rich cases lost the required [rich_message] JSON prefix.
2. Replaced JSON serialization with [rich_message] only: 2 failed, 2 passed; both rich cases failed the prefix/JSON assertion.
3. Removed the empty fallback marker: 1 failed, 3 passed; unknown-empty produced an empty prompt instead of [unhandled message: unknown].
4. Added a second enqueue call: 4 failed; every case reported Awaited 2 times instead of once.
5. Replaced ordinary text/caption fallback with the marker: 1 failed, 3 passed; unknown-with-text became [unhandled message: unknown] instead of legacy text.
6. Diff inspection reported only handlers.py modified; no ChatState/media/activity-test files changed. Restored focused acceptance remained 19 passed.

## Review

Review: none — Sol not authorized. The planned adversarial self-check/mutation matrix was completed; no Luna substitute or production deployment was used.

Memory: none — no reusable lesson.
