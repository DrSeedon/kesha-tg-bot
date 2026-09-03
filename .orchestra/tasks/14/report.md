# Task #14 — implementation report

## Status

Production is untouched. T1–T3 are implemented and green on the target runtime
(167 passed): the night-only scheduler, durable activity tracking, task #13
transaction preservation, the authoritative daytime admission reserve, and the
secret-safe handoff prompt. T4 (production promotion) is not performed and
remains gated on merge approval.

No Codex review was run for this round — the quota is exhausted until
2026-08-05. A documented self-review stands in its place; see
"Self-review performed instead of Codex" below.

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

## Final test run (target runtime)

The suite is green on the exact production runtime:

```text
167 passed in 9.07s
```

Interpreter resolution (this was the blocker, and it was environmental, not code):

- the project has no `pyproject.toml`; deps are declared in `requirements.txt`;
- the checked-out `.venv` at `/mnt/data/Projects/Python/kesha-tg-bot/.venv`
  had **no pytest**, **no `sqlite_vec`/`fastembed`/`watchfiles`**, and shipped
  **`claude-agent-sdk 0.1.50`** while production runs **0.2.128**;
- `uv`'s global `~/.config/uv/uv.toml` sets `exclude-newer = "7 days"`, which
  silently hides `claude-agent-sdk 0.2.128` from the resolver and makes the pin
  look nonexistent. Installing with an explicit `--exclude-newer 2030-01-01`
  override fixes it without editing the global config.

Command that reproduces the green run:

```bash
/mnt/data/Projects/Python/kesha-tg-bot/.venv/bin/python -m pytest -q
```

On SDK 0.1.50 twelve tests fail with
`TypeError: ResultMessage.__init__() got an unexpected keyword argument
'api_error_status'`. That is a stale-environment signal, not a defect: the
`api_error_status`/`model_usage` fields exist only in 0.2.128, which is what
production has (verified over SSH: `sdk 0.2.128`).

## Self-review performed instead of Codex

Codex quota is exhausted until 2026-08-05, so no review round was run and none
is claimed. The following checks were executed directly:

- `run_native_manual_compact` is absent from all non-doc Python (falsified
  native path removed, per T1 AC);
- `compact.maybe_auto_compact()` is absent — no duplicate automatic owner
  remains (T2 AC);
- `claude_session.py:253` sets `env={"DISABLE_AUTO_COMPACT": "1"}`;
- night window verified by executing `_is_auto_compact_night` over all 24
  hours: it admits exactly local hours `[0,1,2,3,4,5,6,7,23]`, i.e. the
  wrapping `23:00–08:00` Krasnoyarsk interval;
- `_seconds_until_night` returns `0.0` inside the window and `39600.0`
  (11 h) at 12:00 Krasnoyarsk;
- `check_context_reserve` is fail-closed: connect failure, non-dict usage,
  malformed totals, wrong model, and `isAutoCompactEnabled != False` all
  reject *before* any query;
- the `_context_reserve_blocked` latch is set only on `reason == "reserve"`
  (`unknown` does not latch) and is cleared on both `/clear`
  (`chat_state.py:283`) and successful compact (`chat_state.py:796`), so a chat
  cannot be permanently stuck;
- AST parse of all nine touched modules succeeds and `COMPACT_PROMPT` is intact
  (3601 chars).

Minor observation, deliberately not changed to keep the diff surgical: the
`/clear` path clears `_context_reserve_blocked` outside `self._lock` while the
other two sites hold it. It is a single boolean write occurring after the phase
has already been reset under the lock, so no ordering bug follows from it.

## Streaming-freeze bug verdict — NOT in #14's scope

Reported symptom: Kesha streams via `edit_message_text`, the user sends a
message mid-stream, editing freezes and later resumes.

The orchestrator's hypothesis (ingress blocks the stream via `session.inject()`
or a shared lock) is **refuted by construction**:

- `session.inject` is called from **no ingress path at all**. Grep over
  `chat_state.py` and `handlers.py` finds zero references; #14 replaced mid-turn
  injection with pure deferral (`chat_state.py:149-160`).
- `accept_entry` holds `ChatState._lock` only for in-memory list appends and
  performs no network await inside it (`chat_state.py:143-163`).
- `ClaudeSession.send_message` releases `_query_lock` at
  `claude_session.py:316-319`, *before* entering the
  `async for msg in self._client.receive_messages()` loop at line 322. The
  streaming loop never holds the query lock, so `inject` could not stall it even
  if it were still called.

The actual mechanism is the third hypothesis — **Telegram flood control on
edits**, and it is pre-existing behaviour, not a #14 regression:

- `response_stream.py:25` — `STREAM_EDIT_INTERVAL = 1.0`, throttling edits to
  roughly Telegram's ~20/min ceiling;
- `response_stream.py:126-131` — on a `Flood control ... retry after N` error
  the handler sets `edit_flood_until = now + wait_sec + 1` and logs
  `Edit flood control, pausing updates for {wait_sec}s`;
- `response_stream.py:111-112` — while `now < edit_flood_until`, `_edit_update`
  returns immediately, so visible edits stop;
- once the deadline passes, the next chunk edits again with the full
  accumulated text — exactly the reported "freezes, then wakes up and finishes".

Note the handler is non-blocking: it records a deadline and returns rather than
sleeping, so the event loop and the Claude turn keep running throughout. Only
the *visible* edits pause. A user typing during the stream makes this more
likely to be noticed because their message and the bot's edits contend for the
same chat's rate budget, but the user's message is not what blocks the loop.

Verdict: real, user-visible, and worth a separate task (options: back off edit
cadence adaptively, or fall back to appending a new message instead of editing
when flood-limited). It is **outside #14's scope** and was not fixed here.

Regression guard added anyway, since #14 owns the ingress path:
`tests/test_activity_ingress.py::test_ingress_during_processing_defers_without_blocking_the_stream`
asserts that five mid-turn arrivals are all deferred, that `session.inject` is
never awaited, and that a concurrent edit loop keeps ticking while ingress is
admitted.

## Remaining

T4 production deployment only, after merge approval. Not performed here.

## Breaking/migration notes

- SQLite migration remains additive (`chat_activity`); old code ignores it.
- No session-file format change.
- Daytime behavior intentionally changes near the reserve: an LLM batch is
  terminally rejected before Claude mutation and asks for `/compact` then
  resend.
- Plain non-LLM commands remain available.
