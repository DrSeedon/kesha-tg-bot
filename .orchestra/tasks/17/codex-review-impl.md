The flood-control approach introduces an avoidable terminal delay and repeated send/delete churn for long flooded responses. The test suite passes, but these user-visible runtime regressions are not covered.

Full review comments:

- [P2] Skip throttling when the live text is already final — /mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/fix-stream-flood/response_stream.py:246-249
  For a plain response whose last delta was already edited successfully, `chunk_text == last_edit_text` and there are no entities, but finalization still enters the budgeted edit. Because that last edit advanced `_next_edit_at`, `_ask_inner` waits up to 3.1 seconds only to issue an unchanged edit, delaying completion and processing of deferred user messages; apply the existing equality shortcut outside the flood-only branch.

- [P2] Compare the visible slice before replacing a flooded stream — /mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/fix-stream-flood/response_stream.py:185-187
  When an edit flood is active and the accumulated response exceeds `TG_MSG_LIMIT`, every later delta changes full `text` while the visible `text[:TG_MSG_LIMIT]` remains identical. This condition therefore sends and deletes the same 4096-character message every three seconds, causing needless notifications and API traffic while already flood-limited; deduplicate using the truncated text actually sent.

## Round 2

### Re-review status

- **FIXED** — Finalization skips the budgeted edit when the plain first chunk already matches the visible live text.
- **FIXED** — Flood fallback stores and compares the actual `TG_MSG_LIMIT` visible slice, so suffix-only growth is deduplicated.

### New findings

None. No new blocking or P2 issues found in `response_stream.py` or `tests/test_response_limit.py`.

### Verdict

**APPROVED.** Targeted tests: 11 passed. Full suite: 171 passed. `git diff --check`: clean.

## Round (2026-08-01T06:52:34Z)

Даже flood control иногда можно научить не флудить 🙃

## Re-review status

- **FIXED** — unchanged plain finalization no longer waits or re-edits.
- **FIXED** — flood fallback compares the visible `TG_MSG_LIMIT` slice.

## New findings

None. No new blocking/P2 issues in the requested scope.

## Verdict

**APPROVED**

- Targeted: 11 passed
- Full suite: 171 passed
- `git diff --check`: clean
- Round 2 appended to [codex-review-impl.md](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/fix-stream-flood/docs/tasks/17/codex-review-impl.md)

Теперь шлюз регулирует поток, а не хлопает дверью каждые три секунды.
