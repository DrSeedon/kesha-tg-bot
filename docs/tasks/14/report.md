# Task #14 — implementation report (WIP, architecture revision)

## Status

Production is untouched. T1–T3 implementation for the accepted night scheduler,
durable activity, task #13 transaction preservation, and secret-safe handoff
prompt exists in the worker diff. Promotion is blocked pending approval and
implementation of the revised daytime admission-reserve plan.

The revised plan passed the resumed Codex adversarial review with
`blocking=0`, `suggestion=0`, and `question=0`. The last accepted review round
is in `docs/tasks/14/codex-review-plan.md`.

## Exact-runtime evidence

Target:

```text
claude-agent-sdk 0.2.128
Claude Code 2.1.220
claude-opus-5[1m]
```

### Native manual compact falsification

An isolated OAuth session reported `compact` in `system/init.slash_commands`.
Submitting `/compact Preserve MANUAL-BOUNDARY-SMOKE.` through the persistent
Agent SDK produced a successful terminal Result but no
`compact_boundary(trigger="manual")`. The implementation correctly returned
`missing_manual_boundary`; the temporary SID remained durable and no automatic
boundary violation was observed.

Conclusion: Agent SDK `query("/compact ...")` cannot be accepted as proof of
context reduction. The revised plan deletes `run_native_manual_compact` and
uses measured pre-admission headroom for task #13's custom compact.

### Reserve measurement

Three isolated target-runtime generations measured the exact accepted
`COMPACT_PROMPT`:

| Case | Before total | Prompt delta | Output | maxTokens | maxOutputTokens |
|---|---:|---:|---:|---:|---:|
| secret/idempotent pre-save | 9,530 | 1,622 | 1,470 | 1,000,000 | 64,000 |
| command evidence | 9,428 | 1,622 | 1,347 | 1,000,000 | 64,000 |
| temporal state | 9,470 | 1,622 | 1,436 | 1,000,000 | 64,000 |

All three reported `isAutoCompactEnabled=false`, `stop_reason=end_turn`, and
non-error terminal Results.

Derived plan constants:

- measured minimum custom-summary headroom: 65,622 tokens;
- manual compact floor after 20% uncertainty margin and upward rounding:
  80,000 tokens;
- normal daytime admission base: 208,000 tokens plus the assembled prompt's
  UTF-8 byte length.

## Prompt evaluation evidence

`docs/tasks/14/compact-eval.json` is the immutable v1 failed checkpoint:

```text
entries=30
completed=27
passed=0
failed=27
incomplete=3
```

It caught two real problems:

- 24 summaries used Markdown headings while the validator accepted only bare
  headings; the revised prompt requires `##` and the validator accepts the
  semantically equivalent bare/Markdown structural form.
- the three former hard-context cells could not prove native manual
  compaction; they are replaced in v2 by
  reserve rejection → custom compact → candidate resume.

The v1 cells remain immutable and are not counted toward the v2 gate. The
resumable runner uses deterministic seed `task-14-compact-v2`, atomic
per-attempt checkpoints, overload-only bounded backoff, and immutable completed
cells. The required v2 promotion gate remains 30/30 with all source-ledger,
secret, file-state, and fabrication checks unchanged.

## Test evidence before plan revision

```text
focused evaluator/prompt: 26 passed
focused T1–T3:             89 passed
full suite:               143 passed in 11.53s
```

These results cover the WIP implementation before removal of the falsified
native path and addition of reserve admission. They are not final acceptance
evidence for the revised implementation.

## Remaining

1. Codex delta-review of the revised research/plan to blocking=0.
2. Orchestrator approval of the revised plan.
3. Remove native manual fallback; implement authoritative admission reserve and
   race-safe deferred preflight.
4. Replace the v1 hard-context fixture, run focused/full tests, then complete
   immutable v2 30/30 live evaluation.
5. Codex implementation review to blocking=0, commit/push, merge approval, and
   only then T4 production deployment.

## Breaking/migration notes

- SQLite migration remains additive (`chat_activity`); old code ignores it.
- No session-file format change.
- Daytime behavior intentionally changes near the reserve: an LLM batch is
  terminally rejected before Claude mutation and asks for `/compact` then
  resend.
- Plain non-LLM commands remain available.
