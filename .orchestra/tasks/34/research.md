# #34 — Automatic compact at admission and continuation of the same batch

**Date:** 2026-09-03

**Phase:** 1 — research only; production untouched; no live Claude/Codex turn

**Current tree:** `1e0a765431c2c8ea223f65b2c83a76503246edfe`

## Question

### Context

Kesha is a single-node Telegram bot whose per-chat `ChatState` serializes turns for two
runtime implementations. Claude uses Kesha's summary → candidate session → durable SID
replacement transaction. Codex uses the app-server's native `thread/compact/start` and keeps
the same thread ID. Task #14 disabled Claude native auto-compaction and replaced the old 95%
post-response trigger with a night-only 55-minute/20% preventive compact plus a predictive
208K admission reserve. Task #24 fixed the Codex post-compact gauge so the first turn after a
verified compact is admitted even though its new usage cannot yet be measured.

### Change under test

Remove the manual context-reserve rejection UX. When the admitted incoming batch would reach
the compaction boundary, Kesha must compact automatically and continue that exact batch once,
without asking the user to resend.

### Baseline

The current baseline is a pre-query reserve rejection at approximately 79.2% for Claude and
approximately 90.7% for Codex (before prompt-size adjustments), a sticky in-process reserve
latch, manual `/compact`, a Claude-only night preventive timer, and no current post-response
percentage check.

### Measurable outcome

A successful design must show, for each runtime:

1. the admitted batch remains owned by one coroutine while compaction runs;
2. runtime query count for that batch is exactly one after successful compaction;
3. compact count for that admission episode is at most one;
4. arrivals during compaction remain deferred and cannot consume the first post-compact turn;
5. compact/query never overlap;
6. compact failure, unchanged post-compact context, or provider rejection cannot loop;
7. no `/compact`-then-resend terminal is emitted at the threshold.

## Incident boundary supplied by the user

- On 2026-09-03 at 20:00 and 22:55, Kesha rejected an incoming message with the current
  “send `/compact`” reserve UX.
- At 23:09 the user manually ran `/compact`; the displayed context was 79%.
- This report does not independently read production logs. The timestamp/79% observations are
  treated as direct user evidence, as requested.
- The local fake-client probe reproduced the boundary exactly: at `totalTokens=790000`,
  `remaining=210000`; a 2,000-byte prompt has `required=210000` and is admitted, while a
  2,001-byte prompt has `required=210001` and is rejected. No query is made.[2]

The 79% display is therefore consistent with predictive 208K headroom and is not evidence of
a 95% threshold firing.

## Evidence table

The rows below record mechanisms and observations only. Synthesis follows the table.

| mechanism/config | exact file:symbol | trigger/threshold | runtime(s) | phase/lock owner | current user-visible behavior | same-batch fate | measured/tested failure modes | existing oracle/test | historical reason/date |
|---|---|---|---|---|---|---|---|---|---|
| `DISABLE_AUTO_COMPACT` | `claude_session.py:ClaudeSession._make_options` (`:330-362`) | Always sets `env={"DISABLE_AUTO_COMPACT": "1"}` for every Claude CLI process | Claude | Runtime construction; no `ChatState` lock | No native compact UI/event from Kesha; Kesha's own compact notices remain | Native CLI never owns an admitted batch; application paths decide before query | Official Claude docs say the flag leaves manual `/compact` available; current focused suite with SDK 0.2.152 passes the flag assertion | `tests/test_claude_session_limit.py::test_options_disable_native_auto_compact` | #14, 2026-07-30: one application owner was required by the then-approved night-only rule; the Agent SDK `/compact` query had produced a normal Result without `compact_boundary` |
| Claude `check_context_reserve` | `claude_session.py:ClaudeSession.check_context_reserve` (`:722-834`) | `remaining >= 208000 + len(combined.encode("utf-8"))`; requires fresh positive usage, 1M raw/effective window, expected model, auto-compact false | Claude | Called by `ChatState._run_batch` while phase is `PROCESSING`; control I/O outside `_lock` | On `reason=reserve`, localized “send `/compact`, then repeat message” | Rejected before `log_user` and `_ask_fn`; `_run_batch` returns and the batch is absent from `pending`/`deferred` | Fake probe at displayed 79%: 2,000 bytes passes, 2,001 rejects; `queries=0`. Fresh zero/None, stale SID, wrong model/window/auto flag fail closed | `test_reserve_exact_boundary_and_one_below`; `test_reserve_never_uses_previous_cache_*`; `test_rejected_batch_gets_one_terminal_and_zero_query` | #14, 2026-07-30: retain 80K custom-summary floor plus two 64K output/agent envelopes; reject rather than risk losing manual compact headroom |
| Codex `check_context_reserve` | `codex_session.py:CodexSession.check_context_reserve` (`:958-995`) | `remaining >= 24000 + _estimate_tokens(combined)`; bootstrap unknown usage admits | Codex | Called by `ChatState._run_batch` in `PROCESSING`; `_connect` outside `_lock` | Same shared reserve terminal as Claude | Known-near-full batch is rejected before `turn/start`; unknown post-compact gauge admits one turn | Historical production `236056/258400` (91.4%) left only 22,344 and reserve-blocked; fake probe reproduces `reason=reserve` | `test_reserve_blocks_when_context_is_nearly_full`; `test_reserve_is_open_but_honest_before_any_turn`; `test_compact_releases_precompact_high_water_before_next_message` | #16, 2026-08-01: mirror Claude's output/prompt headroom; #24, 2026-08-17: unknown after verified native compact must admit the next turn |
| `_context_reserve_blocked` | `chat_state.py:ChatState._context_reserve_blocked`, `mark_context_reserve_blocked` (`:144`, `:222-224`) | Set only after `reason=reserve` or terminal context limit; short-circuits later preflights | Shared | Boolean is normally mutated under `ChatState._lock`; `/clear` and runtime switch have direct writes outside the helper | Every later LLM batch gets the reserve terminal without probing | Each later batch is terminally acknowledged and removed; none is queued for automatic continuation | Fake probe: latch true after one rejection with `pending=0`, `deferred=0`; stale Codex gauge formerly relatching immediately after `/compact` | `test_reserve_latch_short_circuit_does_not_trigger_a_retry`; `test_successful_manual_and_night_compact_clear_reserve_latch`; `test_clear_resets_context_reserve_latch` | #14, 2026-07-30: avoid repeatedly probing/hammering a context already measured as below the 208K manual-recovery reserve |
| Nightly 55m/20% preventive timer | `chat_state.py:_arm_auto_compact`, `_run_auto_compact_scheduler`, `_reserve_automatic_probe` (`:825-979`); `config.py:AUTO_COMPACT_*` (`:35-39`) | Quiescent for 55 min, `[23:00,08:00)` Krasnoyarsk, measured `percentage >=20`, once per durable activity episode | Claude only; `_arm_auto_compact` returns for `native_compact=True` runtimes | Scheduler reserves phase `IDLE→COMPACTING` under `_lock`, probes/compacts outside it; SQLite `claim_auto_attempt` is restart gate | Progress edit, summary message, completion edit; can run far below 95% | It has no incoming batch; arrivals during probe/compact are deferred and drained after the attempt | Daytime 20/95/100 never compacts; activity during probe cancels; cancellation stays disarmed after restart; timer deliberately absent on Codex | `tests/test_preventive_compact.py`; `test_preventive_timer_stays_on_for_claude`; `test_preventive_timer_is_off_for_native_runtimes` | #14, 2026-07-30: compact Claude while 60-minute prompt cache was still warm; 55 min measured 7.5% false rate and 20% was the 19.4% economic break-even |
| Startup recovery of preventive timer | `chat_state.py:ChatRegistry.start_auto_compact` (`:1484-1491`); `message_log.py:list_quiescent_chat_ids` (`:132-136`) | Bot startup + durable quiescent chat + non-empty active runtime session file | Claude scheduler is armed; native runtimes are filtered by `_arm_auto_compact` | Registry bootstrap; each `ChatState` owns its scheduler | Same night compact UI if the restored timer later becomes eligible | No admitted batch is restored; only an idle timer is restored | A claimed episode stays disarmed across cancellation/restart; missing session file is skipped | `test_claimed_episode_stays_disarmed_after_cancellation_and_restart`; `tests/test_chat_activity.py` | #14, 2026-07-30: in-memory timers disappeared across restart, so activity/claim state moved to SQLite |
| Manual `/compact` dispatch | `handlers.py:h_compact` (`:223-240`); `chat_state.py:request_compact`, `_do_compact` (`:658-701`, `:1233-1298`) | Explicit command; manual reserve floor is 80K Claude / 12K Codex; time-independent | Both, dispatched by class `CAPABILITIES.native_compact` | Immediate `IDLE/COLLECTING→COMPACTING` or sticky request during `PROCESSING/STOPPING`; mutations under `_lock`, compact await outside | Progress + success/failure; if busy, “scheduled after processing” | Pending entries are moved to `deferred`; current processing finishes first; manual compact itself has no original user batch to continue | Below manual floor gives one terminal and no compact query; cancel/error releases chat; manual provenance wins a timer race | `test_manual_compact_works_during_day`; `test_manual_compact_below_floor_is_one_terminal_without_query`; `test_manual_during_low_probe_wins_and_uses_custom_compact` | #13/#14, July 2026: retain operator recovery independent of automatic policy and preserve old SID on failed replacement |
| Claude replacement transaction | `compact.py:compact_session` (`:285-421`); `claude_session.py:begin/start/commit/rollback_session_replacement` (`:216-303`) | Application compact primitive selected when `native_compact=False` | Claude | Caller holds phase `COMPACTING`; summary, candidate, commit/rollback run outside `_lock`; session object enforces one active replacement | Progress edit; validated summary may be sent; final success/failure edit | The primitive knows no incoming batch; `ChatState._do_compact` drains deferred in `finally` | Summary/limit/context error and pre-commit cancellation roll back durable old SID; post-commit cancellation keeps candidate; failed source-summary turn still mutated the old transcript | `tests/test_compact_limit.py`; `test_claude_still_uses_keshas_own_compaction` | #13/#14, July 2026: native `/compact` through SDK query was not observable/reliable; candidate SID must be durable only after validated continuation |
| Codex native compact | `chat_state.py:_do_native_compact` (`:1183-1231`); `codex_session.py:compact_context` (`:1057-1135`) | Explicit `thread/compact/start`; success requires matching non-empty `contextCompaction` started/completed item IDs | Codex | Caller phase `COMPACTING`; adapter refuses `_active_turn_id`; RPC/event wait outside `ChatState._lock` | Native progress + success; after percentage is explicitly “measured after next message” | Primitive knows no incoming batch; successful compact leaves usage unknown for whichever batch runs next | Initial implementation returned on interrupted `turn/completed`; #24 showed false `91.4→91.4`; stale/mismatched items could falsely prove completion; timeout/error/cancel tears down process and keeps old gauge/thread ID | `test_compact_waits_past_the_interrupt_for_the_real_compaction`; `test_compact_requires_the_current_started_item_to_complete`; `test_compact_releases_precompact_high_water_before_next_message`; timeout/error/cancel tests | #16, 2026-08-01: Codex exposes no summary text or replacement SID; #24, 2026-08-17: verified completion invalidates stale high-water instead of inventing post-usage |
| Current `_do_compact` finalizer | `chat_state.py:ChatState._do_compact` (`:1233-1298`) | Runs after every manual/deferred/night request, regardless of compact outcome | Both | Clears flags under `_lock`, then always calls `_drain_or_idle` | Returns chat to IDLE or starts deferred work | Fake probe nested inside a hypothetical preflight dispatched `LATER` and left phase `PROCESSING` before an outer owner could resume `ORIGINAL` | First post-Codex-compact unknown admission can be consumed by a deferred batch; using this helper inside `_run_batch` creates two processing owners | `test_a_failed_compact_still_releases_the_chat`; `test_cancelled_native_compact_releases_the_chat`; task #34 fake probe | Current shape inherited from command/timer compaction; it was not designed as a sub-transaction of one admitted batch |
| Post-response automatic checks | `chat_state.py:ChatState._finish_processing` (`:1117-1137`) | Only a pre-existing `compact_requested` flag; no context-percentage read | Both | `_finish_processing` reads flags under `_lock`; optional compact follows outside | A manual/automatic request made during processing runs after the response | A reserve-rejected batch never reaches a response; ordinary deferred batches drain afterward | Literal scan finds no `AUTO_COMPACT_PCT`, `_maybe_auto_compact`, or `compact.maybe_auto_compact` in current Python | Absence protected indirectly by `test_daytime_never_compacts_at_any_context` and current focused suite | #14, 2026-07-30: old immediate post-response 95% path raced deferred input and duplicated the preventive/native owners, so it was removed |
| Unhealthy-control recovery | `claude_session.py:_probe_context_usage`, `check_context_reserve` (`:688-720`, `:765-786`); `chat_state.py:_run_batch` (`:1023-1038`) | Two 10s shielded control probes time out → drop client/preserve SID; `ChatState` repeats the whole reserve check once | Claude | Runtime control tasks outside `_lock`; admitted batch remains local in `_run_batch` | No notice if rebuilt client passes; otherwise one `runtime_unhealthy` terminal | Same original batch reaches `_ask_fn` if the second high-level check passes; zero query/log before that | Naive outer cancellation leaked SDK pending responses; retrying `reserve`/quota was forbidden; current retry has a hard ceiling | `test_probe_timeout_does_not_leak_pending_control_entries`; `test_runtime_unhealthy_retries_once_and_succeeds_silently`; `test_other_reasons_are_never_retried` | #20, 2026-08-02 and #25: healthy control was 0.9–3.4s, production timeouts were exact 60s; reconnect must heal without losing SID |
| Terminal context-limit recovery | `claude_session.py:send_message` (`:564-605`, `:645-659`); `codex_session.py:_error_chunk` (`:901-915`); `response_stream.py:_handle_context_limit` and retry loop (`:416-499`, `:583-646`) | Typed/text `context_limit` from an already submitted runtime turn | Both | Runtime stream active in `PROCESSING`; response owner terminalizes UI and sets reserve latch | Replaces raw error/partial bubble with one `/compact` instruction | No automatic compact and no replay; the submitted batch is not retained for continuation | Context-limit with partial usage must not poison Claude runtime invariant; no retry avoids replay after possible partial effects | `test_context_limit_result_is_one_normalized_terminal_chunk`; `test_context_limit_is_one_manual_compact_outcome`; `test_context_limit_result_with_partial_usage_does_not_latch` | #14, 2026-07-30 and 2026-08-01 incident fix: hard-limit retry could duplicate effects and context-limit payload is not evidence of runtime drift |
| `/clear` recovery | `chat_state.py:request_clear` (`:298-340`); `ChatRegistry.reset_runtime_sessions` (`:1403-1440`) | Explicit command while not `PROCESSING/COMPACTING/STOPPING` | Both runtime session pointers | Clears pending/deferred/flags under `_lock`; resets all runtimes outside it | “cleared” or busy/failure terminal | Intentionally deletes queued conversational work and starts fresh; it never continues the rejected batch | Durable history floor is written first; both provider session pointers are cleared so a switch cannot resurrect old history | `test_clear_resets_context_reserve_latch`; runtime-switch/reset tests | #14/#16, 2026-07 to 2026-08: explicit destructive recovery only; fail-loud if activity persistence/reset fails |
| Codex post-compact gauge recovery (#24) | `codex_session.py:compact_context` (`:1124-1135`) and `check_context_reserve` (`:984-995`) | Verified matching compact completion sets `_context_tokens=None`, `_last_ctx_usage=None` | Codex | Adapter mutation after event proof; next `ChatState` preflight sees unknown | Success says usage will be measured after next message | Exactly the next selected batch is admitted without measured headroom and repopulates gauge from its token event | Before fix, stale 236056 gauge immediately reblocked; failure/cancel must not clear unverified high-water | `test_compact_releases_precompact_high_water_before_next_message`; `test_compact_timeout_disconnects_without_clearing_unverified_usage` | #24, 2026-08-17 production incident: app-server had compacted correctly, but adapter repeated stale 91.4% and latched again |

## Hypotheses considered and falsifiers

### H1 — Native Claude auto-compact can guarantee the same batch survives at 95%

**Claim:** removing `DISABLE_AUTO_COMPACT` is sufficient; Claude CLI will compact at 95% and
continue the submitted SDK query.

**Falsifier:** any supported SDK/CLI path that misses compaction, hangs the response iterator,
compacts at a different threshold, or overflows after a tool/input batch.

**Result: REFUTED.** Current official docs describe auto-compact as “approaching” the limit,
not a same-batch transaction. `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` can only lower the built-in
percentage; values above the default are ignored. The latest SDK type exposes
`autoCompactThreshold`, but the Python client only forwards `get_context_usage`; it does not
offer an atomic “compact then query this input” API.[8][10] Independent upstream reports show:
auto-compact not firing before `Prompt exceeds max length` on recent Agent SDK versions, the
iterator hanging when a first resumed query triggers compact, and custom/parallel tool results
crossing the limit without an intervening compact.[11][12][13] A guarantee is disproved by any
one of these paths; no live provider turn was needed or authorized.

**Confidence: CONFIRMED REFUTATION** — official controls plus three independent upstream
counterexamples; task #14's target-runtime experiment separately refuted treating an SDK
`query("/compact ...")` Result as proof of compaction.[4]

### H2 — An orchestration-level preflight can compact atomically and resume the admitted batch

**Claim:** `ChatState` can retain the admitted `batch`, serialize a runtime-specific compact,
then issue exactly one query using the same assembled prompt.

**Falsifier:** a fake runtime where arrivals race into query, the compact helper drains deferred
work before the original batch, the original prompt is sent zero/two times, or compact repeats
after an unchanged post-state.

**Result: LIKELY, with one mandatory seam change.** The executable model passed for both
runtime transaction shapes with event order
`measure → compact → measure → send(original)` and exactly one compact/send. Its failure case
retained the same batch object and stopped after one compact attempt.[2] The naive reuse of
current `_do_compact` failed the falsifier: it dispatched `LATER` from `deferred` and left phase
`PROCESSING` before the outer owner could resume `ORIGINAL`. Therefore feasibility depends on
separating the compact primitive from chat finalization/draining.

For Claude, post-compact measurement is available. For Codex, #24 establishes a different
success oracle: matching `contextCompaction` completion invalidates usage, and the original
batch must receive the one honest unknown-usage admission. Requiring a fresh Codex percentage
before that turn would deadlock; letting `_drain_or_idle` choose a deferred batch would give the
authorization to the wrong consumer.

**Confidence: LIKELY** — direct fake-runtime execution and existing transaction tests prove the
necessary pieces, but the composed transaction is not implemented and has no frozen acceptance
test yet.

### H3 — Post-response-only compact is enough

**Claim:** let the current turn complete, then compact when usage is at least 95%.

**Falsifier:** an incident path that rejects before any response exists.

**Result: REFUTED.** Current reserve rejection occurs before `log_user` and `_ask_fn`; the fake
probe measured zero query/log and one terminal. `_finish_processing` has no percentage check and
cannot rescue a batch that returned at admission.[1][2]

**Confidence: CONFIRMED REFUTATION** — direct code path plus executable probe.

### H4 — The 208K reserve can simply be deleted without replacement admission behavior

**Claim:** native runtime behavior or the terminal hard-limit handler is enough after deleting
the reserve/latch.

**Falsifier:** evidence that a runtime can cross the limit without compacting, that failed
compact needs pre-existing headroom, or that rejection recovery cannot safely replay the batch.

**Result: REFUTED.** Claude `/compact` can itself fail when too little context remains; task #14
measured an 80K custom-summary floor.[4][9] Claude upstream has missed/hung compaction paths.
Current Codex core explicitly notes that pre-turn auto-compaction checks persisted usage before
recording context updates and the new user message; the open upstream repro shows incoming/tool
data can therefore cross the boundary without triggering compact.[16][18] A blind retry after
provider rejection is not generally safe because partial output/tool effects or a persisted
failed user item may already exist.

**Confidence: CONFIRMED REFUTATION** — official docs/source, historical direct measurement,
and current failure-path tests.

### H5 — The night 55m/20% timer should coexist with the new 95% admission owner

**Claim:** retain the timer unchanged as an independent optimization.

**Falsifier:** it compacts below 95%, applies to only one runtime, or creates a second owner with
different episode/lifecycle semantics.

**Result: REFUTED as an unchanged independent owner.** Its tested trigger is 20%, it is disabled
for Codex, and it owns its own SQLite claim/restart episode. It can therefore compact Claude at
20–94.9% even when no admitted batch exists. The historical rationale was cache economics and a
now-superseded requirement to avoid daytime compact, not same-batch delivery at 95%.[1][4]

**Confidence: CONFIRMED REFUTATION** — current code/tests and historical measurements.

This does not refute reusing its durable activity data for observability. It refutes preserving
the timer as a second automatic compaction trigger.

### H6 — “95%” is current measured usage, predicted current+prompt, or rejection recovery

**Alternatives and falsifiers:**

- **Current measured only.** Falsified as a complete admission rule if an incoming prompt can
  move the next request across the boundary. Current Codex source contains this exact TODO and
  upstream reproduction.[16][18]
- **Predicted current + fully assembled prompt.** Not falsified as the primary Kesha rule, but
  prediction is runtime-dependent and can be stale after Codex tool/output growth. A prediction
  that claims exact tokenizer precision without a runtime oracle would falsify it.
- **Provider rejection recovery.** Falsified as the primary rule because it acts after submission
  and blind replay can duplicate persisted input or side effects. It remains a bounded fallback
  only when the adapter proves the failed turn was pre-output, side-effect-free, and either did
  not persist input or can roll it back.

**Finding:** for Kesha's policy, 95% must be an admission ceiling computed from the freshest
provider-normalized current usage plus a conservative estimate of the fully assembled prompt
(including lazy reminders/media serialization). Provider context rejection is recovery evidence,
not the definition of the threshold. A successful compact must be followed by one bounded
original-batch attempt, never an unbounded “compact until below 95” loop.

**Confidence: LIKELY** — the failure of current-only and rejection-only rules is confirmed;
the exact cross-runtime prompt estimator still needs a Phase 2 oracle.

## Single-owner transaction supported by the evidence

The viable owner is the `ChatState._run_batch` admission coroutine, not Claude/Codex native
auto-compaction and not a post-response callback.

1. Assemble `combined` once, including lazy reminders, while retaining the original `batch`.
2. Under `_lock`, keep the chat non-idle and reserve a distinct compacting substate/flag; await
   usage/compact outside the lock. Every new entry continues to enter `deferred`.
3. Evaluate the 95% admission ceiling from current normalized usage plus prompt estimate.
4. If below the ceiling, proceed to the existing log/query path once.
5. If at/above the ceiling, invoke one runtime-specific compact primitive:
   - Claude: existing validated replacement transaction;
   - Codex: existing matching-item `compact_context` transaction.
6. Do not run `_drain_or_idle` after this internal compact. Restore the same coroutine as the
   sole `PROCESSING` owner.
7. Claude rechecks measurable post-compact usage. Codex accepts verified completion as the
   authorization for this exact original batch while its gauge is unknown, matching #24.
8. Log the user batch immediately before its first runtime query and send the exact `combined`
   once. Only after that response/failure completes may normal deferred draining run.
9. Carry a per-admission `compact_attempted` guard. Compact failure, unchanged/high Claude
   post-usage, or a single prompt still too large terminates without a second compact loop.
10. A typed context rejection may reuse the retained batch only if the runtime adapter proves no
    assistant/tool side effect and no duplicate persisted user input. Current chunk contracts do
    not prove this, so blind response-stream replay is outside the confirmed safe transaction.

This transaction removes the current reserve/latch UX on the success path and gives both runtime
compaction primitives one FSM owner. It also preserves manual `/compact` as an explicit command
serialized by the same state machine.

### Cross-runtime threshold limitation

A literal provider-level “exactly 95% and only one compactor” is not currently available on both
runtimes:

- Claude native compaction can be disabled, but its native percentage override can only lower
  the built-in threshold, not raise it to a requested 95%.[8]
- Current Codex model metadata derives auto-compact at 90% and clamps configured thresholds to
  at most 90%; the current config schema exposes a threshold but no disable flag.[17][19]

Therefore the implementable invariant is: **one Kesha admission coordinator, no duplicate Kesha
compact for one batch, and automatic compaction no later than the 95% Kesha ceiling**. Claude can
obey the Kesha ceiling exactly with native auto disabled. Codex may compact earlier inside its
provider (normally at up to 90%); if Kesha explicitly compacts a measured/predicted ≥95% Codex
thread first, verified completion clears the gauge and normally prevents an immediate second
native compact. Exact 95% parity on Codex remains upstream-constrained.

## Counter-evidence and unresolved edges

- Official Claude docs say native auto-compact normally prevents `Prompt is too long`; this is
  evidence that native compact is useful as defense in depth, but “normally” is weaker than the
  same-batch guarantee and conflicts with current upstream regressions.[9][11-13]
- Current OpenAI app-server tests prove automatic compaction emits matching
  `contextCompaction` lifecycle items. They do not prove Kesha's current usage estimate includes
  the incoming prompt; current core source says it does not.[14-16]
- Codex's unavoidable ≤90% native compaction means “one owner” cannot mean “Kesha is the only
  code anywhere that may compact.” It can mean one Kesha FSM owner and no duplicate explicit
  compact attempt per batch.
- The current admitted batch is memory-only until `log_user` immediately before query. A process
  crash during preflight compact can therefore lose automatic continuation after Telegram has
  acknowledged the update. The requested no-drop invariant across process crash would require a
  durable inbox/outbox record; current #14 activity rows do not store batch content. This is a
  real gap, not solved by the transaction above.
- A single incoming message can itself remain too large after compaction. The one-attempt guard
  prevents a loop, but the bot must surface a different bounded error; it cannot truthfully
  promise continuation for an intrinsically oversized batch.
- Claude replacement summary generation consumes the old context and can have file side effects;
  rollback preserves the old SID, not a byte-identical transcript. This is inherited behavior,
  not introduced by admission compaction.

## Findings and confidence

1. **CONFIRMED — the 03.09 79% incident is the 208K predictive reserve, not a 95% trigger.**
   Tier 1 local fake-client reproduction matches the exact boundary.[2]
2. **CONFIRMED — current reserve rejection does not retain the original batch for automatic
   continuation.** Tier 1 fake `ChatState` run measured zero query/log and empty pending/deferred
   state after one terminal.[2]
3. **CONFIRMED — current `_do_compact` cannot be nested unchanged inside `_run_batch`.** It owns
   deferred draining; the probe dispatched a later batch before the outer original could resume.[2]
4. **LIKELY — an admission-owned, one-attempt transaction can continue the same batch for both
   runtime compact primitives.** Executable model passes and component transactions are heavily
   tested, but composition is not implemented.[2][3]
5. **CONFIRMED — native Claude auto-compact alone cannot provide the requested guarantee.**
   Official control semantics plus multiple independent failure reports refute “guarantee.”[8-13]
6. **CONFIRMED — unchanged night 55m/20% compaction is a second, Claude-only owner below the new
   threshold.** Current source/tests and #14 history agree.[1][4]
7. **CONFIRMED — exact provider-level 95% parity is unavailable on current Codex.** Current source
   derives/clamps auto-compact at 90%, and the schema has no disable flag.[17][19]
8. **UNCERTAIN — crash-durable same-batch continuation.** No current persistence row stores the
   admitted batch body; Phase 2 must either scope the guarantee to process lifetime or add a
   durable delivery record.

## Affected files and risks for a later plan

No implementation is performed in Phase 1. Likely affected surfaces are:

- `chat_state.py` — admission transaction, phase ownership, deferred ordering, old timer/latch;
- `runtime_protocol.py` / `runtime_registry.py` — normalized compact/admission capability rather
  than name checks;
- `claude_session.py` — replace 208K rejection contract while retaining control-channel and
  runtime-invariant recovery;
- `codex_session.py` — verified compact authorization, prompt/current usage normalization, native
  auto limitation;
- `response_stream.py` — remove manual reserve terminal and define typed context-rejection replay
  safety without duplicating effects;
- `config.py`, `handlers.py`, `bot.py`, `message_log.py` — strings, manual command, timer/bootstrap,
  and possibly crash durability;
- focused tests under `tests/test_preventive_compact.py`, `test_compact_dispatch.py`,
  `test_compact_limit.py`, `test_claude_session_limit.py`, `test_codex_session.py`,
  `test_response_limit.py`, and `test_runtime_limits.py`.

Blocking risks are original/deferred reordering, two processing tasks, one compact per runtime
both firing, blind replay after side effects, Codex unknown usage being consumed by the wrong
batch, loss across restart, and a compact/retry loop when one input remains too large.

## Verification performed

- `python3 .orchestra/tasks/34/spikes/flow_probe.py` → exit 0; raw output in
  `.orchestra/tasks/34/spikes/flow_probe.out`.[2]
- Focused suite on current `claude-agent-sdk==0.2.152` / bundled Claude Code 2.1.259:
  `238 passed, 3 skipped in 16.74s`; exact command in
  `.orchestra/tasks/34/spikes/focused-suite.out`.[3]
- Local Codex binary: `codex-cli 0.150.1`. Generated experimental app-server schema confirms
  `thread/compact/start`, empty response object, and `contextCompaction{id}` item.[19]
- Literal current-tree scan: no `AUTO_COMPACT_PCT`, `_maybe_auto_compact`, or
  `compact.maybe_auto_compact` in Python.
- No `uv.lock` was created or modified. No provider query and no production access occurred.

## Sources

1. Current repository source at `1e0a765`: `chat_state.py`, `claude_session.py`,
   `codex_session.py`, `compact.py`, `response_stream.py`, `handlers.py`, `runtime_protocol.py`,
   `runtime_registry.py`, `config.py`, `message_log.py` — evidence tier 2 (primary source).
2. `.orchestra/tasks/34/spikes/flow_probe.py` and `flow_probe.out` — evidence tier 1 (direct
   local fake-runtime measurement, 2026-09-03).
3. `.orchestra/tasks/34/spikes/focused-suite.out` — evidence tier 1 (direct local test run,
   SDK 0.2.152, 2026-09-03).
4. `.orchestra/tasks/14/research.md`, `plan.md`, `report.md`, `codex-review-*.md`, and compact
   evaluation summaries — retained direct measurements and design history, 2026-07-30 to 08-01.
5. `.orchestra/tasks/20/research.md`, `plan.md`, `report.md`, `spikes/*`,
   `codex-review-research.md` — retained control-channel measurements/history, 2026-08-02.
6. `.orchestra/tasks/24/compact-noop.md`, `codex-review-impl.md` — retained production/app-server
   compact evidence and reviewed fix, 2026-08-17.
7. `.orchestra/tasks/16/research.md`, `research-v2-runtime-switch.md`, `deploy-notes.md`,
   `codex-review-*.md`, `spikes/compact_*`, `spikes/appserver_methods.txt`, and turn probes —
   retained runtime contract measurements/history, 2026-08-01.
8. [Claude Code environment variables](https://code.claude.com/docs/en/env-vars) — evidence tier 2
   (official primary docs; opened 2026-09-03).
9. [Claude Code error reference](https://code.claude.com/docs/en/errors) — evidence tier 2
   (official primary docs; opened 2026-09-03).
10. [Claude Agent SDK Python context usage types](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py)
    and [client](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/client.py)
    — evidence tier 2 (official source; opened 2026-09-03). PyPI JSON reported latest 0.2.152,
    bundled CLI source reported 2.1.259.
11. [Agent SDK TypeScript auto-compact regression #381](https://github.com/anthropics/claude-agent-sdk-typescript/issues/381)
    — evidence tier 4 alone; corroborated by sources 12–13.
12. [Agent SDK Python first-query compact hang #288](https://github.com/anthropics/claude-agent-sdk-python/issues/288)
    — evidence tier 4 alone; independent failure shape.
13. [Agent SDK Python missing mid-loop compact #531](https://github.com/anthropics/claude-agent-sdk-python/issues/531)
    — evidence tier 4 alone; independent failure shape.
14. [Codex app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
    — evidence tier 2 (official source/protocol docs; opened 2026-09-03).
15. [Codex app-server compaction tests](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/compaction.rs)
    — evidence tier 2 (official primary tests; opened 2026-09-03).
16. [Codex current turn implementation](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs)
    — evidence tier 2 (official source; fetched/opened 2026-09-03).
17. [Codex model auto-compact limit implementation](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/openai_models.rs)
    — evidence tier 2 (official source; fetched/opened 2026-09-03).
18. [Codex stale incoming/tool usage repro #32888](https://github.com/openai/codex/issues/32888)
    — evidence tier 4 for the report, corroborated by the explicit TODO in source 16.
19. Direct local `codex app-server generate-json-schema --experimental` on `codex-cli 0.150.1`,
    plus official `core/config.schema.json` — evidence tier 1 local schema measurement and tier 2
    source. Schema count: `disable_auto_compact=0`, `model_auto_compact_token_limit=3`.
