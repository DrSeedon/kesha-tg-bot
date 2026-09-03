## Summary

🧯 Naturally, the transaction is vaguest exactly where a crash can eat the session. The plan stays within sensible existing boundaries, but it is incomplete on stream ownership, crash-atomic SID persistence, post-commit behavior, and raw Telegram cleanup.

Static review only; no files changed and no tests run.

## Findings

### blocking: Close injection before yielding the terminal limit outcome

[plan.md:39](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:39) drains currently expected Results before yielding the normalized error, but does not close the injection window. Currently [`inject()`](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:217) checks `_is_processing` before acquiring `_query_lock`; while `send_message()` is suspended at the error yield, a new message can increment `_expected_results`. `_ask_inner()` then breaks, leaving that injected Result unread in the persistent SDK stream. Specify an atomic transition under `_query_lock`: stop accepting injections and recheck that `_expected_results == 0` before yielding. Add AC racing `inject()` against the final Result and proving the next query cannot consume a stale Result.

### blocking: Define limit state per injected Result

[plan.md:32](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:32) describes one pending limit signal even though injection allows several Assistant/Result pairs in one receive loop. The plan does not say when that signal is consumed, how multiple limit Results collapse into one emitted error, or how a later successful Result clears the latch in Result order. Use separate per-Result pending state and stream-level “limit seen” state. The T1 AC must include `_expected_results > 1` mixed sequences; the current single-Result cases at [plan.md:106](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:106) do not cover this required injection semantics.

### blocking: Make SID commit crash-atomic

[plan.md:44](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:44) preserves the old file before commit, but commit merely “writes” the candidate SID. The current [`_save_session()`](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:66) truncates the file in place, so a process crash during commit can leave an empty or partial SID. Require a same-directory temporary file followed by atomic replacement, with in-memory commit occurring only after that succeeds. T2 needs crash/restart AC for before begin, during candidate creation, failed commit, and completed commit; none appears in [its current AC](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:185).

### blocking: Disarm rollback after commit

[plan.md:74](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:74) commits before sending the summary, but [plan.md:77](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:77) still mandates rollback on any `BaseException` without limiting it to the pre-commit region. Cancellation or notifier/context-usage failure after commit could therefore restore the old SID after the candidate was durably accepted. Make rollback a no-op after commit or guard it with explicit transaction state. Add AC cancelling immediately after commit and proving the candidate SID remains active and persisted.

### blocking: Replace any streamed raw limit output

Suppressing the final `TextBlock` at [plan.md:31](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:31) is insufficient with `include_partial_messages=True`: [`send_message()`](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:185) can yield raw `text_delta` events before the typed `AssistantMessage.error`, and [`_ask_inner()`](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:282) immediately exposes them through a live Telegram edit. Sending a separate friendly notice would leave raw output and produce two outcomes. Specify that the limit branch clears buffered text and replaces/deletes the current live message before marking `terminal_handled`. Add a `StreamEvent(raw delta) → AssistantMessage(rate_limit) → ResultMessage` AC for both delivery paths.

### suggestion: Recheck the preventive latch at execution time

[plan.md:97](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:97) checks the latch only before calling `request_compact()`. A user turn can start after that check, causing the preventive request to become the undifferentiated `compact_requested` flag; if that turn hits the limit, [`_finish_processing()`](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/chat_state.py:535) still executes `_do_compact()` without another latch check. Preserve automatic/manual provenance or recheck at the deferred execution boundary, and add this race to T2 AC.

## Verdict

**REJECT**

The planned boundaries are appropriately minimal, but four required orchestrator criteria lack safe mechanics and AC: injection-safe terminal draining, crash-atomic SID replacement, no rollback after commit, and removal of already-streamed raw limit output. The preventive path also has a real latch race.

Right now the compact transaction is a fireproof safe whose lock gets installed after the fire. 🔐

## Round (2026-07-28T06:36:29Z)

## Summary

🧷 Miraculously, defining the commit point made the transaction transactional. All six prior blocking findings are addressed with matching implementation rules and vertical AC coverage.

## Findings

### suggestion: Make manual provenance dominate coalesced requests

[plan.md:120](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/plan.md:120) does not define how the boolean merges repeated deferred requests. A preventive request that passed its initial idle check can arrive after a manual request and overwrite `automatic=False` with `True`; if the current turn then sets the latch, `_finish_processing()` would discard the user-requested compact. Specify that manual provenance is sticky until the queued request is consumed, and add the overlapping manual/preventive race to the AC.

## Verdict

**APPROVED**

No remaining blocking findings. The one provenance rule should be clarified before implementation, but it does not justify rejecting the plan.

The preventive timer just needs to learn that manual requests don’t queue behind it. ⏱️
