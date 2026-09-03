# #33 — limit self-censor fix

## Result

`ResultMessage.result` is now treated as quota text only when the result has
terminal/error evidence (`is_error`, typed 429, `blocking_limit`, or pending
typed limit evidence) or when that result emitted no visible main-agent text.
Visible `AssistantMessage` text and main-agent `StreamEvent` text deltas are
tracked independently for each result. Subagent narration is excluded. The
evidence flag resets after every `ResultMessage`, including injected batches.

Raw-only `is_error=False` limit payloads still classify as `usage_limit` and
remain excluded from runtime-invariant validation. Typed limit evidence remains
unconditional.

## Frozen oracle

Commit `48e38ce` added the initial RED tests before production changes;
`1ee2a5a` added the subagent negative oracle. On the old implementation, the
Assistant and StreamEvent self-censor tests failed with an extra normalized
`usage_limit` error.

## Verification

Review gate inputs:

- Changed files/consumers: `claude_session.py` (`ClaudeSession.send_message`),
  consumed by `response_stream.py`; `tests/test_claude_session_limit.py`;
  this report.
- Author model/runtime: Codex GPT-5 worker; Python asyncio with the repository
  `.venv` and `claude-agent-sdk` test runtime.
- Exact AC: run the command below; preserve successful visible output, classify
  genuine raw-only and typed limits, reset evidence between results, exclude
  subagent narration, and preserve runtime-invariant behavior.
- Named check and observed output: the acceptance command below and its
  `70 passed in 12.50s` result; mutation commands/results are recorded below.

Acceptance command:

```text
/home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_claude_session_limit.py tests/test_response_limit.py tests/test_session_limit.py
70 passed in 11.60s
```

Mutation checks, all run against committed tests and reverted afterward:

| Mutation | Expected result |
| --- | --- |
| Restore unconditional `usage_limit_reset(raw_result)` | RED: 2 self-censor tests failed, rc=1 |
| Remove raw-only detector | RED: `test_usage_limit_result_does_not_latch_runtime_invariant`, rc=1 |
| Remove per-result visible-output reset | RED: injected sequential test, rc=1 |
| Count subagent narration as visible | RED: dedicated subagent oracle, rc=1 |

Review: none — Sol not authorized; no Luna substitute. Adversarial self-check
and final diff read completed. No deployment performed.

## Pre-mortem

- A raw-only genuine limit could stop classifying → existing raw-only and
  partial-usage limit tests remain green in the acceptance suite.
- A typed 429 or `blocking_limit` could be masked by visible output → the two
  visible-before-typed tests pass.
- Visible evidence could leak between injected results → the sequential
  injected-batch test passes and the no-reset mutation turns it RED.
- Internal subagent narration could suppress a real limit → the dedicated
  subagent test passes and the count-subagent mutation turns it RED.
- Runtime invariant could latch on a genuine quota result → the raw-only and
  partial-usage tests pass, including admission recovery assertions.
