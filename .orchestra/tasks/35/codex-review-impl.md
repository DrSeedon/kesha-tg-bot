<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-luna"} -->

## Summary

The pinned diff is correct:

1. No new path sends or logs the batch twice.
2. `_run_batch` and `_do_compact` restore the phase through `finally`.
3. Unmeasured preflight results now continue to delivery; terminal outcomes remain explained.
4. Manual `/compact` still reports success or failure through compact notifications.
5. The `elif` correctly avoids compacting without a measured trigger; no variable is left unbound.

## Findings

ACK — no findings.

Verbatim production line:

> `logger.error("Runtime invariant latch is set: an earlier terminal usage "`

## Verdict

APPROVE.
