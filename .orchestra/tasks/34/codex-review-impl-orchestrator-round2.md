<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-luna"} -->

## Summary

All four questions are resolved:

1. **FIXED** — usage-limit admission no longer silently drops the batch.
2. **FIXED** — no double send, retry loop, phase wedge, or duplicate answer; provider limit handling remains terminal and bounded.
3. **STILL POSSIBLE, BUT VISIBLE** — a latched limit can allow a prompt that exceeds remaining context to reach the provider; the provider’s context-limit terminal is shown to the user.
4. **FIXED** — no accidental silent-drop path remains in `_run_batch`; other compact failures emit their bounded terminal first.

## Findings

No blocking or actionable findings.

The removed reserve-recovery evaluator is consistent with the new admission contract: normal turns no longer reject on the obsolete reserve condition.

## Verdict

**APPROVED**

Verbatim changed production line:

> `trigger_tokens = int(raw_maximum * AUTO_COMPACT_TRIGGER_PCT / 100)`
