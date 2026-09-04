# #34 — Phase 2 review evidence

## Decision gate inputs

- Changed Phase 2 files/consumers: eight test modules, `plan.md`, and RED evidence.
  Planned Phase 3 consumers are shared `ChatState` queue/lock/message delivery,
  Claude/Codex session pressure and compact lifecycle, response retry terminals, and
  bootstrap scheduler ownership.
- Author metadata: Orchestra exposes the author runtime as Codex; the exact model ID is
  not exposed in this worker's task context and is not inferred from the worker name.
- Exact AC: the frozen product invariant and T1–T3 ticket AC in `plan.md`.
- Named checks and observed output: T1 `7 failed`, T2 `1 failed`, T3 `7 failed/1 passed`,
  broader focused `22 failed, 217 passed, 3 skipped`; exact commands and first assertions
  are in `red-oracles.md`.

## Route

The plan changes shared runtime/session/message delivery, queue/lock ordering, and an
admission gate. This sets the high-risk floor: the canonical model-review route is an
auxiliary Sol pass. No auxiliary Sol run was authorized. Per the explicit assignment,
no Luna substitute was started.

**Review: none — Sol not authorized.**

## Mechanical plan checks

- Three tickets are vertical and acyclic: T1 → T2 → T3.
- Every ticket has a committed RED command at immutable oracle baseline `58c3f31`.
- Each command exits 1 on a missing-behavior assertion, not an import/collection error.
- The plan names every production/test/artifact file and explicit non-goals.
- The plan resolves 95% vs 80K numerically and freezes Claude 92% / Codex 90%.
- Manual `/compact`, no-blind-replay, original/deferred ordering, and one-attempt failure
  paths each have a named assertion.
- Existing timer/reserve tests were replaced before implementation because their old
  assertions encoded the superseded product behavior; Phase 3 is not permitted to edit
  any test or fixture.

## Adversarial self-review

1. **Could exact 95 be restored by lowering max output?** Only with a process-wide output
   change or an unmeasured compact-only reconnect/profile transaction. Both expand the
   runtime/session invariant surface and are not covered by existing evidence. Choosing
   92 preserves the verified primitive and is the latest safe trigger.
2. **Could 92 still be reached after a prior turn has already grown past it?** Yes. The
   admission transaction still attempts exactly one compact; if headroom is already too
   low or post-state remains high, it terminates once without `/compact`/resend. The plan
   does not claim every intrinsically overfull state is recoverable.
3. **Does retaining the method name `check_context_reserve` preserve the removed UX?** No.
   Normal semantics become a pressure decision (`ok=True, should_compact`); only explicit
   manual `/compact` retains the lower-floor reserve result. Latch, normal reserve branch,
   and user string are removed by source oracle.
4. **Can Codex double compact?** Kesha explicitly compacts known projected ≥90% pressure
   before `turn/start`; verified completion clears the gauge, so the original unknown turn
   normally does not trigger another provider compact. Codex native auto remains an
   unavoidable fallback, so the plan promises one Kesha compact attempt, not sole control
   of provider internals.
5. **Can automatic failure create two Telegram terminals?** The compact primitive owns its
   own progress/failure message, so `_run_batch` emits no second terminal for `ok=False`.
   Only successful compact followed by high/oversized pressure gets the separate batch-not-sent
   terminal.
6. **Can deferred input consume Codex's unknown admission?** The compact primitive is split
   from `_drain_or_idle`; the T2 oracle blocks original completion and proves deferred work
   has not started while the original owns `usage=None`.
7. **Can response-level context errors be replayed safely?** No; the plan explicitly leaves
   them terminal and asserts one runtime call. Crash/replay is already tracked outside scope.

No unresolved blocking contradiction remains in the plan. The mutation matrix in `plan.md`
is the required adversarial implementation review in the absence of authorized Sol.
