# #34 — Phase 1 review evidence

## Decision gate

- Changed artifacts/consumers: research and KB only; conclusions concern shared
  `ChatState` message delivery, runtime session replacement, queue/lock ordering, and
  admission/recovery gates.
- Author runtime: current Orchestra worker session; no reviewer identity inferred from name.
- Exact Phase 1 AC: required ten-column mechanism table; all named mechanisms; current code,
  tests, #14/#16/#20/#24 history; fake-runtime probes; no production/provider mutation;
  `research.md` + promoted KB fact.
- Named checks:
  - table parser → `columns_exact True count 10`, `table_rows 15`;
  - `python3 .orchestra/tasks/34/spikes/flow_probe.py` → exit 0;
  - focused current-SDK suite → `238 passed, 3 skipped in 16.74s`;
  - `python3 -m compileall -q .orchestra/tasks/34/spikes/flow_probe.py` → exit 0;
  - `git diff --check` → exit 0.

## Route

Shared session/message-delivery and admission/concurrency surfaces set the high-risk review
floor. The canonical route would be Sol, but an auxiliary Sol run was not authorized and the
task explicitly prohibited live provider turns without approval. No substitute reviewer was
started.

**Review: none — Sol not authorized; live provider turn not approved.**

## Mechanical completeness

- Evidence-table columns exactly match the ten requested names and order.
- Explicit table rows exist for `DISABLE_AUTO_COMPACT`, both runtime reserve checks,
  `_context_reserve_blocked`, night 55m/20%, manual `/compact`, Claude replacement, Codex
  native compact, current compact finalizer, post-response checks, unhealthy recovery,
  terminal context-limit recovery, `/clear`, startup recovery, and #24 gauge recovery.
- Every required hypothesis has a claim/falsifier/result/confidence entry.
- The incident's 79% interpretation has a direct executable boundary measurement.
- The KB gained current facts, rejected roads, explicit gaps, and a README entry.
- The repository does not contain `scripts/check_kb_contract.py`; the prescribed validator
  could not run. New/changed KB fact lines were checked manually for stable `fact:` keys,
  `искать:` anchors, self-contained wording, same-line evidence, and dates.

## Adversarial self-review

1. **Could native Claude still be sufficient in the exact deployed version?** The research does
   not claim every native turn fails; it refutes a guarantee. Official threshold controls do not
   expose an exact raise-to-95 contract, and independent same-family SDK failures are enough to
   falsify sufficiency. No change needed.
2. **Does the fake transaction prove the implementation?** No. The artifact labels the composed
   result `LIKELY`, identifies the missing `_do_compact` seam, and requires a frozen Phase 2
   oracle. No overclaim found.
3. **Does unknown Codex usage after compact weaken fail-closed admission?** It is an existing,
   reviewed #24 exception backed by matching completion; the research narrows that exception to
   the retained original batch instead of allowing deferred work to consume it.
4. **Could “remove the night timer” erase a valuable optimization?** Yes, cache economics are
   counter-evidence. The conclusion is limited to removing it as an unchanged independent 20%
   compaction owner; activity data may remain. The user-approved behavior and single-owner rule
   outweigh preserving a second trigger.
5. **Can exact 95% be promised on Codex?** No. The report surfaces the upstream 90% clamp as a
   confirmed limitation and defines “no later than Kesha's 95% ceiling,” rather than hiding the
   mismatch.
6. **Is crash durability solved?** No. The report explicitly leaves it `UNCERTAIN` because
   `chat_activity` does not persist the batch body. This is a Phase 2 scope decision, not silently
   assumed atomicity.
7. **Could provider-error replay duplicate effects?** Yes. The report rejects blind replay and
   requires proof/rollback before using a rejection fallback.

No unrecorded blocking hole remains in the Phase 1 conclusion. Open gaps are promoted in
`.orchestra/kb/context-compaction.md` rather than treated as solved.
