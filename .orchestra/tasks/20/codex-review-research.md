# #20 — Codex review: NOT PERFORMED

**There is no Codex verdict for this task. Nothing below substitutes for one.**

## Why

`codex_review` was invoked (`mode="exec"`, target `docs/tasks/20/research.md`) with an adversarial
prompt asking Codex to falsify the four load-bearing claims. The job failed immediately:

```
{"type":"turn.started"}
{"type":"error","message":"You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage
 to purchase more credits or try again at Aug 8th, 2026 12:53 PM."}
{"type":"turn.failed"}
```

Quota is exhausted until **2026-08-08**. This is the documented empty-quota failure mode, not an
infrastructure or prompt problem.

Orchestrator decision: do not retry, and do **not** substitute a different reviewer — a stand-in
review "for the checkbox" produces false confidence, which is worse than an acknowledged gap.

## What was asked of Codex (unanswered)

1. Is "SIGSTOP reproduces prod" an unjustified leap? What else yields an exactly-60.0 s control
   timeout that the fix would not help — e.g. a dropped `control_response` frame on a healthy
   process, or a dead stdout reader task? If the frame is simply lost, does the retry help at all?
2. Does bounding the call from outside leak SDK state?
3. Is `_last_ctx_usage` really stale-and-too-small?
4. Does the proposal ever admit a message the current code would refuse?

## Self-review in place of the verdict

- **(2) — answered by measurement, and it found a real defect in my own proposal.** The naive
  `asyncio.wait_for` leaked one `pending_control_responses` entry per timeout (0→1→2→3, never
  reclaimed; `spikes/ctxleak.py`). Shipping recommendation #1 as first written would have slowly
  poisoned a session that lives for weeks. Fixed via shield + orphaned task; verified back to 0
  (`spikes/ctxleak2.py`). Independently confirmed by the orchestrator against `query.py:588-590`.
- **(4) — covered by test.** `test_full_context_still_refused_when_probe_works` plus mutation M4
  (fail-open → 3 tests red). No path admits without a fresh successful measurement.
- **(3) — verified in source**, not memory: `check_context_reserve` calls the client directly and
  never writes the cache; the only writer is `get_context_usage()`, driven by the 55-min idle
  compact timer. Orchestrator independently confirmed the same four line references.
- **(1) — REMAINS THE WEAKEST POINT, genuinely unreviewed.** SIGSTOP reproduces the *signature*
  (60.1 s, then healthy on the next call) but does not prove prod's cause is a stopped process. A
  lost `control_response` frame on an otherwise healthy CLI would present identically to our code.
  The fix degrades acceptably in that case — the retry is a fresh request with a new `request_id`,
  and after two failures the reconnect rebuilds the transport, which is the correct response to a
  lost frame too. So the fix is not *contingent* on the SIGSTOP interpretation. What is unverified
  is the claim "the CLI was stopped/wedged in prod"; the honest statement is "the control channel
  stopped answering for ≥60 s, cause not established".
