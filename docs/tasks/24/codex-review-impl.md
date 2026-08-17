<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-sol"} -->

The stale usage invalidation addresses the production reserve-block, but compact completion is not correlated with the operation that was just started. A queued completion can therefore cause false success and unsafe usage invalidation.

Review comment:

- [P1] Require the matching compaction item before declaring success — /home/kesha/orchestra/worktrees/home-kesha-projects-kesha-tg-bot/fix-codex-compact/codex_session.py:977-982
  issue: When a stale or unrelated `item/completed` notification for any `contextCompaction` is already queued, this branch accepts it without requiring the current operation's `item/started` or matching its ID. The method then clears the usage gauge and ChatState reports success even though this compact may still be running or unchanged. Track the started item ID and only accept a completion with that same non-empty ID; stale notifications should be ignored.

> ⚠ Codex usage unaccounted: ValueError: Codex completed turn reported zero tokens

## Author review log

- Round 1 spent: substantive P1 response, but reviewer omitted `## Verdict`.
  Finding disposition: **ACK** after checking the queue consumer at
  `codex_session.py:976-982`.
- Before round 2: stale queued notifications are discarded before the request;
  only a non-empty `item/started` id followed by the same `item/completed` id
  can complete the operation. A frozen regression and mutation both fail on
  the old/mismatched behavior. Round 2 resumed this same review session.

## Round (2026-08-17T11:11:01Z)

<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-sol"} -->

## Round 2

Re-review status: prior P1 is **FIXED**.

## Findings

No new blocking bugs in the reviewed compaction-notification seam.

Concrete evidence:

- Queued non-process notifications are discarded before `thread/compact/start`.
- Only a non-empty `contextCompaction` `item/started` ID is recorded.
- `item/completed` succeeds only when `item_id == compaction_item`.
- The regression includes a stale pair and mismatched completion before the valid matching completion.
- `py_compile` and `git diff --check` pass locally.

Verbatim changed line:

```python
if compaction_item and item_id == compaction_item:
```

The focused suite was not rerun because this worktree has no `.venv`; the supplied result is 142 passed, 1 skipped.

## Verdict

**APPROVED**
