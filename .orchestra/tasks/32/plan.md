# Task #32 plan: preserve forwarded rich messages

## Scope

Change only the fallback boundary in handlers.py and add the focused acceptance oracle in tests/test_rich_message.py. The existing enqueue -> ChatState.accept_entry() -> debounce -> batch/message-log flow remains the single pipeline.

For a non-None msg.rich_message, h_fallback will pass this exact prompt representation once:

    [rich_message] <msg.rich_message.model_dump_json(exclude_none=True)>

This is a plain string. It keeps every non-null field parsed by installed aiogram 3.30.0, including empty blocks, nested rich text, structural fields, and recognized nested media descriptions.

For a fallback message without rich_message:

- non-empty msg.text or msg.caption continues to be passed unchanged once;
- an empty fallback gets the explicit marker [unhandled message: <content_type>] once, so the update is visible to the existing model/log pipeline rather than discarded.

The marker is tested with the ordinary unknown fixture whose content_type is the string unknown. No new parser, dispatcher filter, media download path, ChatState API, debounce rule, or message-log schema is introduced.

## Files and symbols

- handlers.py:h_fallback — branch on msg.rich_message before the existing text/caption fallback; serialize with aiogram's parsed model; preserve access control and warning logging; always call enqueue exactly once for an allowed fallback update.
- tests/test_rich_message.py:test_t1_fallback_handles_rich_and_unknown_messages — immutable parameterized oracle for valid rich content, empty rich blocks, unknown-with-text compatibility, unknown-empty explicit admission, and exactly-once enqueue.
- tests/test_activity_ingress.py — read-only regression consumer; do not edit.

## What is explicitly not changed

- handlers.py: h_text, all media handlers, register() ordering, enqueue() batching semantics, and forward/reply metadata helpers.
- chat_state.py, message_log.py, telegram_io.py, aiogram dependency/version, fixtures outside test_rich_message.py, and production deployment.
- Unknown rich block discriminators that aiogram 3.30.0 rejects before handler dispatch; supporting newer Bot API blocks is a separate schema-compatibility task.

## Tickets

### T1 — Admit rich messages and make empty fallback explicit

- Files: handlers.py:h_fallback; tests/test_rich_message.py
- Test: tests/test_rich_message.py::test_t1_fallback_handles_rich_and_unknown_messages — committed RED in 4e6ee65
  - command: /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_rich_message.py::test_t1_fallback_handles_rich_and_unknown_messages
  - current result: exit 1; first failing assertion is AssertionError: Expected mock to have been awaited once. Awaited 0 times. for the rich-paragraph case
- AC: /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_rich_message.py tests/test_activity_ingress.py is green; the rich paragraph and empty-block cases each invoke enqueue exactly once with the [rich_message] JSON representation; the JSON equals the parsed model dump; unknown-with-text still passes the original text unchanged once; unknown-empty passes [unhandled message: unknown] once.
- blocked-by: none

## Adversarial self-check and mutation plan

Sol is the technical review floor for this shared message-delivery surface, but an auxiliary Sol run is not authorized. Review: none — Sol not authorized. No Luna substitute will be used.

Before accepting the implementation, inspect the diff and run the named acceptance command. Apply these targeted mutations (or equivalent reversible probes) against the committed oracle, then restore the implementation:

1. Remove the msg.rich_message branch or replace its prompt with msg.text/msg.caption: both rich parameters must fail the exactly-once/non-empty JSON assertions.
2. Serialize only block text or omit the JSON prefix: the parsed-model equality assertion must fail, exposing lost media/structural fields.
3. Keep empty unknown fallback un-enqueued or emit an empty marker: the unknown-empty case must fail.
4. Enqueue twice: the exactly-once assertion must fail.
5. Replace unknown-with-text with the explicit marker: the unchanged-text assertion must fail.
6. Confirm no diff in chat_state.py or media/debounce handlers; the full activity ingress suite must remain green.

## Frozen RED oracle

The test was run before production changes:

    /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_rich_message.py -x

    1 failed (stopped at rich-paragraph); enqueue.assert_awaited_once() failed with Awaited 0 times. The failure is the missing rich-message admission, not collection/import failure.

The complete file run also measured 3 failed, 1 passed: rich-paragraph, rich-empty-blocks, and unknown-empty fail for the missing behavior; unknown-with-text remains green compatibility coverage.
