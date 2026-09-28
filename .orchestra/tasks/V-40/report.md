# V-40: refusal fallback, context admission, and resumed-session accounting

## Result

Refusal-based switching is disabled in the Claude CLI subprocess with
`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`. The fallback event remains handled as a defensive
path: `SystemMessage(subtype="model_refusal_fallback")` causes a Russian warning containing
the actual response model, then `ClaudeSDKClient.set_model()` restores the configured model
before the next user request. If restoration fails, the next request is blocked with a
visible error rather than sent to the fallback model.

The stream-specific refusal path is still delivered when the CLI ends on the configured
model. The context reserve accepts the known `claude-opus-4-8` fallback only when its
reported window remains exactly 1M and CLI auto-compact is disabled. Response accounting
uses the main `AssistantMessage.model`. When a `ClaudeSession` resumes a saved session,
`_ensure_connected()` seeds cost and token counters from the transcript's most recent
`cost-state` record before the next answer.

## Fallback control evidence

The production SDK bundle at
`/opt/kesha-bot/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude`
reported CLI 2.1.280 in the supplied production inventory. Read-only `rg -a -o` against
that binary found `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` (3 matches),
`switchModelsOnFlag` (14), and `model_refusal_fallback` (35). This is direct evidence that
the deployed binary contains the env switch and setting. Claude Code's model docs state
that `switchModelsOnFlag=false` pauses instead of switching, and that SDK/non-interactive
integrations end a flagged turn with a refusal. The documented per-session control is
`switchModelsOnFlag`; the environment counterpart is `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK`
(also noted in [the upstream settings issue](https://github.com/anthropics/claude-code/issues/75913)).
The official [model configuration docs](https://code.claude.com/docs/en/model-config#ask-before-switching)
cover the refusal behavior. The pinned [SDK v0.2.158 client](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.158/src/claude_agent_sdk/client.py#L1956-L1991)
exposes `set_model()` for streaming clients.

No live safety refusal was induced in production. The setting is passed via the same
`ClaudeAgentOptions.env` path already used for `DISABLE_AUTO_COMPACT`; tests use the actual
stream message classes, including `SystemMessage`, rather than one-shot query exceptions.

## Cost diagnosis from production

The eight rows named in the task all correspond to stream results after `Connecting with
resume` in journal logs:

| `response_usage.id` | DB cost | Logged resumed session | Result log / process |
| ---: | ---: | --- | --- |
| 368 | $109.7713 | `f4a37d4e` | PID 1892760, 4 turns |
| 370 | $106.0295 | `176ca71b` | PID 2038361, 3 turns |
| 371 | $52.9149 | `1a45fc68` | PID 2038361, 1 turn |
| 372 | $106.3412 | `176ca71b` | PID 2052793, 6 turns |
| 378 | $58.0462 | `1a45fc68` | PID 2052793, 4 turns |
| 434 | $98.4874 | `1a45fc68` | PID 2796266, 3 turns |
| 457 | $148.0504 | `176ca71b` | PID 2796266, 3 turns |
| 496 | $164.9580 | `176ca71b` | PID 2871885, 16 turns |

The database and transcript directly agree on the full running total and cumulative token
counters for three independent resumes: row 368 matches `f4a37d4e` `cost-state` at
$109.7712506, output 155226 and cache-read 124304093; row 370 matches `176ca71b` at
$106.0294688, output 158974 and cache-read 121576904; row 371 matches `1a45fc68` at
$52.9149272, output 141043 and cache-read 83336056. These are not plausible single-answer
costs; each row recorded the transcript's session accumulator. The journal also shows the
same resume-before-result pattern for rows 372, 378, 434, 457, and 496. The logger
process IDs changed between these periods, consistent with restarts; lazy session creation
also reproduces the defect without a process restart.

Cause: `ClaudeSession.__init__()` started `_session_cost_seen` and
`_session_model_usage_seen` at zero even when `_load_session()` restored a session ID.
`_absorb_result_cost()` and `_absorb_result_model_usage()` then subtracted zero from the
CLI's cumulative session state on the first resumed result. The previous delta logic only
worked when the same Python object had observed the prior result. Loading the latest JSONL
`cost-state` before the next request establishes the baseline across both restarts and
lazy per-chat session creation.

## Tests and mutation evidence

Focused run on the final source: 90 passed across
`tests/test_claude_session_limit.py`, `tests/test_response_usage.py`, and
`tests/test_response_limit.py`. The added tests cover the disabled env, streaming fallback
warning and model restoration, visible refusal on the stream path, fallback context
measurement, actual model persistence, and resumed cost/token deltas.

Six temporary mutations each made the corresponding committed-for-this-change regression
test fail: env value `1→0`; refusal subtype no longer matched; fallback allowlist emptied;
response row reverted to configured model; resumed-baseline load bypassed; warning delivery
removed. Each mutation was reverted immediately after its single focused test. The final
full acceptance command from the task completed with `672 passed, 3 skipped in 40.73s`
using `mcp==1.28.1` and `claude-agent-sdk==0.2.158`; raw output is saved as
[`full-suite.log`](full-suite.log).

Production remained read-only throughout; no deploy or restart was performed.
