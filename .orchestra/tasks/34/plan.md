# #34 — Phase 2 plan: admission-owned automatic context compaction

**Date:** 2026-09-04

**Status:** plan and frozen RED oracles only; no production implementation

**Immutable oracle baseline:** `58c3f31` (includes the initial RED commit
`e1e2a8b`, one-attempt/replay hardening, and T3 grouping commits)

## Frozen product invariant

- No normal incoming batch receives a manual context-reserve terminal, `/compact`
  instruction, or resend request.
- The admission owner retains the original `batch` and assembled `combined` prompt,
  performs at most one automatic compact, and sends that original prompt exactly once
  after successful compaction.
- Every arrival during automatic compaction remains deferred until the original batch's
  runtime turn completes.
- The independent night 55-minute/20% trigger no longer exists.
- Manual `/compact` remains an explicit operator recovery path.
- An already-submitted provider `context_limit` is terminal and is never blindly replayed.
- Crash-durable replay, safe provider-rejection replay, literal Codex 95%, and byte-identical
  Claude rollback are outside this task and already live in `TODO.md` on local `main`
  (`32b6d76`).

## Threshold decision and exact math

### Claude: 92%, not 95%

The current verified compact primitive cannot safely start at a literal 95%:

```text
1M context at 95% used                  = 50,000 remaining
measured compact prompt/input delta     =  1,622
runtime maxOutputTokens                 = 64,000
measured minimum request headroom       = 65,622
20% safety margin, rounded upward       = 80,000
1M context at 92% used                  = 80,000 remaining
```

`MANUAL_COMPACT_FLOOR_TOKENS=80_000` is therefore retained. The new automatic
Claude trigger is the latest verified-safe value:

```python
AUTO_COMPACT_TRIGGER_PCT = 92.0
projected_tokens = totalTokens + len(combined.encode("utf-8"))
should_compact = projected_tokens >= rawMaxTokens * 0.92
```

This preserves the user's real boundary: at 79% and a 2,001-byte ordinary prompt,
`projected_tokens=792001`, so Kesha sends without rejection or compaction. A prompt
that moves the projected request to 920,000 or higher compacts first.

The documented `CLAUDE_CODE_MAX_OUTPUT_TOKENS` could make the arithmetic fit only by
lowering the process-wide output budget. A hypothetical 32K compact profile gives
`(1,622 + 32,768) × 1.2 = 41,268`, which fits in 50K, but the current Agent SDK has no
per-query max-output option. Applying it to the persistent client would reduce normal
Kesha outputs from 64K; switching only the compact turn would add an unmeasured
disconnect/reconnect, temporary invariant mode, and rollback interaction. Phase 1
explicitly rejected relying on unverified native compaction. Exact 95 is therefore not
a safe Phase 3 change; the plan chooses 92 without altering `compact.py`'s transaction.

### Codex: 90% policy ceiling

Current Codex model metadata derives/clamps native auto-compaction at 90%. Kesha uses
the same ceiling rather than claiming 95% parity:

```python
CODEX_AUTO_COMPACT_TRIGGER_PCT = 90.0
projected_tokens = _context_tokens + _estimate_tokens(combined)
should_compact = projected_tokens >= _context_window * 0.90
```

Unknown usage before a first turn or immediately after verified `contextCompaction`
returns `should_compact=False` without inventing a percentage. After Kesha explicitly
compacts a known high Codex thread, the retained original batch consumes that first
unknown admission. Codex's own native pre-turn compaction remains defense in depth;
there is no supported disable flag in the current schema.

## Runtime result contract

Keep the existing `check_context_reserve(combined="", manual=False)` method to avoid a
wide protocol/factory migration, but change its normal-turn semantics and documentation:

- `manual=True` remains the existing manual compact floor check (80K Claude, 12K Codex),
  including `ok=False, reason="reserve"` for explicit `/compact` that is already too late.
- `manual=False` no longer rejects a healthy near-full context. It returns:

```python
{
    "ok": True,
    "reason": None,
    "should_compact": bool,
    "projected_tokens": int | None,
    "max_tokens": int | None,
    "usage": dict | None,
}
```

- Claude retains the fresh, uncached #20 control probe and every model/window/auto-disable
  invariant. `runtime_unhealthy`, `runtime_invariant`, `session_unavailable`, `usage_limit`,
  and `unknown` remain `ok=False` and never become compact decisions.
- Codex known usage computes the 90% projection; unknown usage is honest
  `ok=True, should_compact=False`.
- `probe_readiness()` treats `should_compact=True` as a ready runtime: the first batch owns
  the compact. Only `ok=False` blocks switching/readiness.

The internal method name is retained for surgical compatibility; the old 208K/24K normal
reserve constants and user UX are removed.

## Single-owner state transaction

### New internal split in `ChatState`

1. `_run_batch` assembles `combined` once and keeps the original `batch` local.
2. It calls `session.check_context_reserve(combined)` once, preserving the existing one
   high-level retry only for `runtime_unhealthy`.
3. `ok=False` keeps the existing typed health/quota/session terminal mapping, except no
   `reason="reserve"` normal path exists.
4. `should_compact=False` continues directly to one `log_user` and one `_ask_fn`.
5. `should_compact=True` transitions `PROCESSING→COMPACTING` under `_lock`; new entries are
   already routed to `deferred` by `accept_entry`.
6. `_compact_once(automatic=True)` dispatches by `CAPABILITIES.native_compact` and returns
   its result. It never clears request flags, calls `_drain_or_idle`, starts another
   processing task, or logs the retained batch.
7. After success, the same `_run_batch` coroutine restores `COMPACTING→PROCESSING` under
   `_lock` and re-runs context pressure exactly once:
   - Claude gets a fresh low measurement;
   - Codex gets the reviewed #24 unknown result and authorizes this original batch.
8. If the recheck still says `should_compact`, the input is intrinsically oversized or
   the compact did not reduce pressure. The per-batch `compact_attempted` guard forbids a
   second compact and terminalizes once.
9. After a successful recheck, the exact original `combined` is logged and sent once.
10. Only `_finish_processing` after that turn may drain `deferred`.

### Manual wrapper

`request_compact()` and `_finish_processing` keep using `_do_compact()` as the explicit
operator wrapper. `_do_compact()` performs the manual floor check, calls
`_compact_once(automatic=False)`, clears command provenance, and then drains/returns idle.
A manual request arriving while an automatic admission compact is already running is
coalesced with that operation; it does not schedule a second compact behind the same batch.

### Failure and user-visible behavior

- Add `context_auto_compact_failed` in Russian and English. It states only that automatic
  compaction could not safely send the message; it contains no `/compact`, “repeat”, or
  “resend”.
- Compact failure, high unchanged context, and a still-oversized prompt perform zero
  runtime query for the retained batch. The compact primitive retains ownership of its own
  progress/failure message; `_run_batch` must not duplicate an `ok=False` terminal. A successful
  compact followed by high/oversized recheck gets one separate
  `context_auto_compact_failed` terminal explaining that the retained batch was not sent.
- `response_stream` retains terminal handling for already-submitted `context_limit` but
  removes the latch and manual `/compact` copy. It does not retry the runtime turn.
- A generic response-stream reconnect retry that fails its pre-retry pressure check emits
  the same non-manual bounded terminal and performs zero second query; it never latches.

## Removing the independent automatic owner

Remove from `chat_state.py`:

- `_is_auto_compact_night`, `_seconds_until_night`;
- `_auto_compact_task`, `_cancel_auto_compact`, `_arm_auto_compact`;
- `_run_auto_compact_scheduler`, `_reserve_automatic_probe`;
- automatic episode/request provenance that exists only for the night scheduler;
- startup/shutdown scheduler restoration.

Remove the call to `ChatRegistry.start_auto_compact()` from `bot.py` and remove the old
window/idle/minimum constants from `config.py`. Keep the additive `chat_activity` table and
its columns for compatibility; this task does not perform a destructive migration. Existing
activity admission/completion writes remain unchanged.

Remove `_context_reserve_blocked`, `mark_context_reserve_blocked`, its clear/switch/compact
writes, and both localized `context_reserve` strings. A source oracle requires the old
normal `reason == "reserve"` branch to disappear from `_run_batch`.

## Full affected-file list

### Production files

- `config.py`
  - add `AUTO_COMPACT_TRIGGER_PCT=92.0` and
    `CODEX_AUTO_COMPACT_TRIGGER_PCT=90.0`;
  - remove night timer constants and `context_reserve` strings;
  - add `context_auto_compact_failed` in both languages;
  - rewrite shared `context_limit` copy without `/compact` or resend language.
- `claude_session.py`
  - keep native auto disabled and manual 80K floor;
  - change normal `check_context_reserve` to fresh projected 92% pressure;
  - remove `NORMAL_TURN_RESERVE_TOKENS=208_000`;
  - preserve #14 runtime invariants and #20 bounded/leak-free recovery.
- `codex_session.py`
  - change known normal pressure to projected 90%; unknown remains open/honest;
  - remove the normal 24K reserve while preserving the 12K manual floor;
  - preserve matching-item completion, failure teardown, thread ID, and #24 gauge invalidation.
- `chat_state.py`
  - add admission-owned one-attempt compact/recheck flow;
  - split compact primitive from manual finalization/draining;
  - remove latch and independent night scheduler lifecycle;
  - preserve original/deferred ordering and manual command behavior.
- `response_stream.py`
  - remove reserve latch calls and manual reserve/context-limit copy;
  - keep provider context rejection non-replayed;
  - map a failed pre-retry pressure check to the bounded automatic-compact terminal.
- `bot.py`
  - remove startup restoration of the deleted scheduler.
- `runtime_protocol.py`, `runtime_registry.py`
  - update comments/docstrings for the changed normal pressure semantics; required method
    surface remains unchanged.

### Frozen/adjusted test files (already committed before implementation)

- `tests/test_auto_compact_admission.py`
- `tests/test_preventive_compact.py`
- `tests/test_compact_dispatch.py`
- `tests/test_claude_session_limit.py`
- `tests/test_codex_session.py`
- `tests/test_response_limit.py`
- `tests/test_runtime_limits.py`
- `tests/test_activity_ingress.py`

### Phase artifacts

- `.orchestra/tasks/34/plan.md`
- `.orchestra/tasks/34/review-plan.md`
- `.orchestra/tasks/34/red-oracles.md`

## Explicit non-goals / files not to change

- Do not change `compact.py`'s `COMPACT_PROMPT`, summary validation/redaction,
  `_SessionReplacement`, or durable SID commit/rollback sequence.
- Do not change `handlers.py` command routing; manual `/compact` already reaches
  `ChatState.request_compact`.
- Do not change `message_log.py` schema or add durable batch replay.
- Do not change runtime switching, provider auth/quota policy, RAG, reminders, media,
  tool bridge, or production state.
- Do not enable Claude native auto-compaction or claim Codex compacts exactly at 95%.
- Do not implement replay after a submitted provider context rejection.

## Tickets

### T1 — Claude admission compacts at the latest safe boundary and retains the batch

- Files: `config.py`, `claude_session.py`, `chat_state.py`,
  `runtime_protocol.py`, `runtime_registry.py`,
  `tests/test_auto_compact_admission.py`, `tests/test_claude_session_limit.py`
- Test: `tests/test_auto_compact_admission.py::test_t1_*` — committed RED in
  `58c3f31`; command:
  `/home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil --with telegramify-markdown python -m pytest -q --tb=short tests/test_auto_compact_admission.py -k test_t1`
  → exit 1, first failing assertion:
  `E AssertionError: the admitted 79% batch was not sent`
- AC: the exact command above is green; `AUTO_COMPACT_TRIGGER_PCT == 92.0`;
  79% + 2,001-byte prompt performs zero compact, zero terminal, one send; projected
  92% performs one compact and one send of the original; arrivals remain behind original;
  compact failure/high unchanged/oversized performs at most one compact, zero send, one
  non-manual terminal; native Claude auto remains disabled; #20 control recovery and manual
  80K floor tests remain green.
- blocked-by: none

### T2 — Codex uses one native compact and gives the unknown admission to original

- Files: `config.py`, `codex_session.py`, `chat_state.py`,
  `tests/test_auto_compact_admission.py`, `tests/test_codex_session.py`,
  `tests/test_compact_dispatch.py`
- Test: `tests/test_auto_compact_admission.py::test_t2_*` — committed RED in
  `58c3f31`; command:
  `/home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil --with telegramify-markdown python -m pytest -q --tb=short tests/test_auto_compact_admission.py -k test_t2`
  → exit 1, first failing assertion:
  `E AssertionError: Codex admission did not compact`
- AC: the exact command above is green; known projected Codex pressure triggers at 90%;
  Kesha calls native `compact_context` exactly once and never the Claude custom primitive;
  the original batch is the sole consumer of post-compact `usage=None`; deferred input starts
  only after original completion; matching-item/failure teardown/#24 stale-gauge tests remain
  green; no exact-95 claim or native-disable setting is added.
- blocked-by: T1

### T3 — Remove legacy reserve/night owners while preserving explicit manual recovery

- Files: `config.py`, `chat_state.py`, `response_stream.py`, `bot.py`,
  `runtime_protocol.py`, `runtime_registry.py`,
  `tests/test_auto_compact_admission.py`, `tests/test_preventive_compact.py`,
  `tests/test_compact_dispatch.py`, `tests/test_response_limit.py`,
  `tests/test_runtime_limits.py`, `tests/test_activity_ingress.py`
- Test: `tests/test_auto_compact_admission.py::test_t3_*`,
  `tests/test_response_limit.py::test_t3_*`, and
  `tests/test_activity_ingress.py::test_t3_*` — committed RED in
  `58c3f31`; command:
  `/home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil --with telegramify-markdown python -m pytest -q --tb=short tests/test_auto_compact_admission.py tests/test_response_limit.py tests/test_activity_ingress.py -k test_t3`
  → exit 1, first failing assertion:
  `E AssertionError: the independent night scheduler still armed`
- AC: the exact command above is green; no scheduler task is armed for the old
  55m/20% episode; `_run_batch` has no normal `reason == "reserve"` branch; latch method/state
  and localized `context_reserve` keys are absent; provider context-limit is terminal with
  exactly one runtime call and no `/compact`/repeat/resend; manual `/compact` still performs
  one runtime-appropriate compact; activity persistence/schema remains compatible.
- blocked-by: T1, T2

## Focused and final regression commands

After every ticket, run its exact named command plus:

```bash
/home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project \
  --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group \
  --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil \
  --with telegramify-markdown python -m pytest -q --tb=short \
  tests/test_auto_compact_admission.py tests/test_preventive_compact.py \
  tests/test_compact_dispatch.py tests/test_compact_limit.py \
  tests/test_claude_session_limit.py tests/test_codex_session.py \
  tests/test_response_limit.py tests/test_runtime_limits.py \
  tests/test_activity_ingress.py tests/test_runtime_protocol.py
```

Final project suite, with no lockfile mutation permitted:

```bash
/home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project \
  --with-requirements requirements.txt --with pytest --with pytest-asyncio \
  python -m pytest -x -q
```

## Adversarial mutation matrix

| Mutation | Oracle that must turn RED |
|---|---|
| Restore `208_000 + prompt bytes` as normal rejection | `test_t1_79_percent_ordinary_prompt_is_sent_without_compact_or_rejection` |
| Set Claude trigger to 95 or any value later than 92 | `test_t1_claude_trigger_is_the_latest_verified_safe_92_percent` |
| Ignore assembled prompt size and use current percentage only | `test_t1_predicted_boundary_compacts_once_then_sends_original_once` |
| Skip automatic compact at projected boundary | `test_t1_predicted_boundary_compacts_once_then_sends_original_once` |
| Reuse current `_do_compact` so it drains `deferred` | `test_t1_arrival_during_compact_stays_behind_original` |
| Loop a second custom/native compact after failure/high recheck | hard ceilings in `CompactDriver.__call__` / `Runtime.compact_context` plus `test_t1_failure_*` |
| Send after failed/unchanged/oversized compact | `test_t1_failure_is_one_bounded_terminal_with_zero_send_or_loop` |
| Give Codex's first unknown admission to a deferred batch | `test_t2_codex_original_consumes_first_unknown_post_compact_admission` |
| Route Codex through Claude custom replacement | same T2 test (`custom.calls == 0`) |
| Restore night scheduler arm | `test_t3_night_55m_20pct_no_longer_arms_or_compacts` |
| Keep the reserve branch/latch/string | `test_t3_legacy_reserve_terminal_and_latch_are_removed` |
| Replay a provider context rejection | `test_t3_context_limit_is_one_non_replayed_terminal` (`session.calls == 1`) |
| Restore `/compact` or resend wording | T1 failure tests and `test_t3_context_limit_is_one_non_replayed_terminal` |
| Break explicit manual `/compact` | `test_t3_manual_compact_remains_explicit_operator_recovery` |
| Enable Claude native auto-compaction | `test_options_disable_native_auto_compact` |

## Final RED evidence

Oracle baseline `58c3f31`:

`python -m pytest -q tests/test_auto_compact_admission.py -k test_t1` → exit 1,
`E AssertionError: the admitted 79% batch was not sent`
