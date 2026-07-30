# Task #14 — Phase 2 plan: night-only automatic compact

**Status:** plan only; no implementation or production changes
**Accepted research:** `docs/tasks/14/research.md`
**Target runtime:** Python asyncio/aiogram, `claude-agent-sdk==0.2.128`,
bundled Claude Code `2.1.220`, single-node Contabo

## Outcome

Kesha will have exactly one application-owned automatic compaction policy:

```text
automatic compact =
    local time in [23:00, 08:00) Asia/Krasnoyarsk
    AND durable inactivity >= 55 minutes
    AND durable quiescent=true
    AND ChatState is atomically IDLE with no pending/deferred/media work
    AND known context >= 20%
    AND usage_limit_active is false
```

Claude Code native auto-compaction will be disabled at the SDK subprocess
boundary. The old immediate 95% path and preventive timer will become one
restart-safe per-chat scheduler. Manual `/compact` will remain available at any
time and retain task #13's transactional SID/limit behavior.

## Assumptions and decisions

1. **The night window is fixed product behavior, not an operator-tunable
   threshold.** Use `ZoneInfo("Asia/Krasnoyarsk")`, start inclusive and end
   exclusive. No environment override can silently turn daytime back on.
2. **Known context is authoritative, not inferred.** The night scheduler uses
   `ClaudeSDKClient.get_context_usage()` on the resumed, connected session.
   After restart it performs this probe only after reserving the otherwise-idle
   chat. A missing/zero/failed result is unknown and fails closed; no percentage
   is reconstructed from timestamps or process uptime.
3. **Activity lifecycle is transactional enough for restart safety.** Admission
   writes `quiescent=0` before state mutation; only a full return to `IDLE`
   writes `quiescent=1`. A crash leaves `0` and disables automatic compact after
   restart.
4. **The scheduler never uses `request_compact(automatic=True)` as a deferred
   queue.** It reserves `COMPACTING` only after a final check under the same
   `ChatState._lock`. If activity/manual work won the lock first, automatic
   compact is rescheduled rather than queued behind the turn.
5. **Prompt quality has two gates.** Deterministic fixture scoring covers
   sections, exact anchors, redactions, paths/commands/numbers, recent text, and
   file diffs. Open-world fabrication/bloat cannot be proven by a parser, so
   every live output also receives a source-ledger audit. Both are hard
   promotion gates.

## Runtime design

### Durable activity row

`message_log.MessageLog.__init__()` adds an additive table:

```sql
CREATE TABLE IF NOT EXISTS chat_activity (
    chat_id                INTEGER PRIMARY KEY,
    last_activity_utc      TEXT NOT NULL,
    quiescent              INTEGER NOT NULL CHECK (quiescent IN (0, 1)),
    auto_attempted_for_utc TEXT
);
```

New flat methods on `MessageLog`:

- `begin_activity(chat_id, now_utc=None)` — atomic upsert of
  `last_activity_utc=now`, `quiescent=0`, and clears
  `auto_attempted_for_utc`.
- `finish_activity(chat_id, now_utc=None)` — atomic upsert of
  `last_activity_utc=now`, `quiescent=1`, and clears
  `auto_attempted_for_utc`.
- `claim_auto_attempt(chat_id, last_activity_utc)` — one conditional update
  that sets `auto_attempted_for_utc=last_activity_utc` only while the durable
  row is still the same unclaimed quiescent idle episode.
- `get_activity(chat_id)` and `list_quiescent_chat_ids()` — read-only scheduler
  inputs.

Admission persistence is not swallowed:

- `ChatState.accept_entry()` and `run_urgent_prompt()` call
  `begin_activity()` before queue/inject/phase mutation.
- The existing transcription lifecycle becomes the equally small
  `media_started()` / `media_finished()` lifecycle. Every async media handler
  calls `media_started()` after authorization but before its first
  download/transcription `await`; it persists `begin_activity()` before
  incrementing pending media state. Persistence failure starts neither media
  work nor entry admission.
- On SQLite failure `message_log.py` raises a small project-local
  `ActivityPersistenceError`; the entry is not admitted.
- `handlers.enqueue()` sends one localized retry message.
- `inbox_server.handle_inbox()` keeps its existing failure boundary but returns
  a generic 503 rather than a raw SQLite error.
- The existing urgent reminder wrapper catches the exception and uses its
  current plain-message fallback; it must not report a Claude delivery that was
  never admitted.

`_drain_or_idle()` writes `finish_activity()` only when all admitted work has
actually drained and the state reaches `IDLE`. If that write fails, the durable
row stays `quiescent=0`; automatic compact remains fail-closed across restart.
Plain reminders never enter Claude/`ChatState` and do not count as activity;
lazy reminders count only when attached to a later admitted user entry.

### One scheduler

Replace `_preventive_task` with one `_auto_compact_task` per `ChatState`.
Relevant helpers remain in `chat_state.py`; no generic scheduling module:

- `_night_window_state(now_utc)` — validates an aware timestamp and evaluates
  `[23:00,08:00)` in `Asia/Krasnoyarsk`.
- `_next_night_open(now_utc)` — calculates the next 23:00 boundary.
- `_arm_auto_compact()` — cancels/replaces the per-chat one-shot task from the
  durable row.
- `_run_auto_compact_scheduler()` — sleeps to the later of the 55-minute
  inactivity deadline and the next window opening, then evaluates eligibility.
- `_reserve_automatic_probe(snapshot)` — under `_lock`, re-reads the durable row
  and verifies it matches the scheduler snapshot, checks exact `IDLE`, empty
  pending/deferred/transcriptions, no manual compact request, current night
  window, and no usage-limit latch. It conditionally claims that durable idle
  episode with `claim_auto_attempt()`; a claim/write failure aborts. Only the
  successful claimant changes `IDLE -> COMPACTING`.
- The reserved task calls a narrow
  `ClaudeSession.get_context_usage(refresh=True, preserve_session=True)`.
  `refresh=True` connects/resumes before querying; `preserve_session=True`
  forbids the normal missing-session invalidation/retry path during this
  eligibility probe. Failure returns unknown and leaves the durable SID
  untouched.
- After the awaited probe, the scheduler rechecks night, durable snapshot,
  usage latch, queued work, and manual provenance under `_lock`. Automatic
  compact starts only for a known percentage >=20. A manual request recorded
  during the probe changes the existing request provenance to manual and
  proceeds regardless of the automatic time/percentage/latch gates.
- The reservation uses the existing request fields:
  `compact_requested=True`, `compact_requested_automatic=True`.
  `request_compact(automatic=False)` gains an explicit `COMPACTING` branch: if
  the in-flight reservation is automatic, it sets
  `compact_requested_automatic=False` instead of returning unchanged. The
  scheduler consumes this sticky provenance only when starting or aborting the
  reserved operation.

Scheduler outcomes:

- outside the window: schedule the next 23:00 opening, do not drop the task;
- activity/busy/manual race: activity/manual wins; re-arm after the later
  transition to quiescent `IDLE`;
- context `<20%` or unknown: release `COMPACTING` without compact and disarm
  until later activity;
- non-quiescent/missing row: fail closed;
- usage-limit latch: disarm until a successful later turn clears the latch and
  finishes activity;
- compact limit/error: one terminal attempt for that idle episode, no loop;
- compact success: complete the task and wait for new activity before any new
  automatic attempt.

`auto_attempted_for_utc == last_activity_utc` disarms that exact idle episode
after low/unknown context, limit/error, success, cancellation, crash, or
restart. Only the next durable activity changes `last_activity_utc` and clears
the marker. Claiming before network I/O deliberately favors a skipped
automatic attempt over a restart loop.

The final lock defines the race boundary: activity durably admitted before
reservation cancels automatic compact. Activity arriving after the reserved
`IDLE -> COMPACTING` transition is deferred by the existing COMPACTING
behavior; the post-probe lock sees it and cancels the automatic attempt. A
manual `/compact` arriving after reservation sets the existing sticky
`compact_requested_automatic=False`; if compaction has already started, that
same transaction fulfills the manual request rather than scheduling a second
one.

### Trigger convergence and restart

- Remove `ChatState._maybe_auto_compact()` as an execution path.
- Remove `_on_preventive_elapsed()` and the duplicated 55-minute decision.
- Remove `compact.maybe_auto_compact()` and its constructor/wiring dependency.
- After a response, `_run_batch()` never checks a threshold or compacts.
  Returning to quiescent `IDLE` only persists the completion timestamp.
- `/clear` and successful manual compact re-arm/disarm from the next real
  activity cycle; neither leaves a stale automatic request.
- `ChatRegistry.start_auto_compact()` enumerates `chat_activity` chat IDs that
  have numeric session files, creates their states, and arms only rows with
  `quiescent=1`. It never treats `ALLOWED` user IDs as chat IDs.
- `bot.main()` calls this startup method after registry wiring and before
  polling.
- `ChatRegistry.shutdown()` cancels the one scheduler task per chat.

On migration, existing sessions have no `chat_activity` row and therefore do
not auto-compact until their first successful post-upgrade activity cycle. This
is deliberately backward-compatible and fail-closed.

### SDK ownership and daytime hard-limit result

`ClaudeSession._make_options()` supplies:

```python
options.env = {"DISABLE_AUTO_COMPACT": "1"}
```

The SDK merges this with the inherited process environment; proxy/auth values
are not copied or hardcoded.

`ClaudeSession.send_message()` gains two narrow normalizations:

- An unsolicited `SystemMessage(subtype="compact_boundary", trigger="auto")`
  logs a critical invariant violation with trigger metadata. An explicitly
  awaited manual boundary from the recovery primitive below is expected and
  scoped to that call.
- official hard-context variants (`Prompt is too long`,
  `Context exceeds ... token limit`, and the matching terminal result/status)
  yield one terminal `{"type":"error","kind":"context_limit",...}` and are never
  reconnected/retried or treated as a dead session.

`response_stream._ask_inner()` handles `context_limit` using the same terminal
UI discipline as task #13:

- clear/replace any streamed raw text;
- finalize tool status/timer;
- edit the current message or send one fallback;
- mark `terminal_handled=True`;
- suppress raw error, `empty`, and `📋`;
- localized text tells the user that context is full and asks for
  `/compact`.

No automatic compact is called from this error branch.

### Full-context manual recovery

The ordinary/manual path remains task #13's custom
summary → validated handoff → candidate session → atomic SID commit. It does
not switch to native compaction pre-emptively.

If and only if the first summary request of a manual `/compact` terminates with
`kind="context_limit"`, use Claude Code's documented SDK slash-command escape
hatch:

1. `ClaudeSession.run_native_manual_compact(instructions)` sends
   `/compact <instructions>` directly through the current persistent client,
   reconnecting/resuming with the same preserve-session rule if the preceding
   hard-context error dropped the transport. It drains the response and
   succeeds only after both
   `compact_boundary(trigger="manual")` and a non-error terminal Result.
2. `instructions` are the same accepted preservation/redaction contract, so
   the unavoidable first-stage summary is focused on the same anchors.
3. After the manual boundary frees context, retry the ordinary task #13 custom
   summary transaction exactly once. Only a valid retry starts the candidate
   and can atomically replace the SID.
4. Limit/error/no-boundary leaves the durable SID untouched and terminalizes
   progress once. If native compaction succeeded but the retry failed, keep the
   same now-compacted SID, report one friendly partial-recovery outcome, and
   never `/clear` or loop.

`automatic=True` never enters this fallback. The slash command is allowed only
for an explicit user `/compact`; therefore a daytime hard-context error cannot
silently trigger compaction.

The primitive is not implemented through the generic `send_message()` error
path: it owns one query/result drain, verifies the manual boundary, and cannot
leave a stale terminal Result in the persistent receive queue.

### Handoff prompt and runtime guard

Replace `compact.COMPACT_PROMPT` with the accepted contract from
`research.md`:

- global secret policy applies to every file write and every summary section;
- typed `[REDACTED SECRET: <type>]` markers override verbatim requirements;
- idempotent pre-save only to an existing canonical Markdown note;
- `CLAUDE.md` only for stable operating rules;
- exact objective, user facts/preferences, decisions, files/artifacts,
  command/tool outcomes, pending/blockers, temporal state,
  uncertainty/conflicts, recent verbatim, and continuation;
- redundant raw tool output is excluded.

Small runtime helpers in `compact.py`:

- `_redact_high_confidence_secrets(summary)` handles PEM private-key blocks,
  common token/key prefixes, and explicit credential assignments. The raw
  assembled summary is immediately replaced by its redacted value before
  validation, length/result accounting, any debug/info log, continuation
  preamble, or Telegram output; raw secrets are never logged;
- `_validate_summary_sections(summary)` requires every accepted top-level
  section in order. It does not treat section-like text inside the untrusted
  `RECENT VERBATIM` payload as a second structural header.

Redaction is defense-in-depth, not a generic DLP framework. Pre-save tool writes
cannot be rewritten server-side, so their security/idempotence is enforced by
the prompt plus the live isolated-file fixture gate.

Malformed/empty summaries produce a friendly terminal failure and do not start
the candidate session. All existing task #13 rollback/cancellation/usage-limit
tests stay authoritative.

## Migration and compatibility

### Database

- Migration is only additive `CREATE TABLE IF NOT EXISTS chat_activity` plus
  the guarded marker-column `ALTER` described below; existing `messages`, RAG
  tables, reminders, WAL settings, and session files are untouched.
- The initial production schema includes nullable `auto_attempted_for_utc`. For
  an intermediate development database created from the earlier two-column
  draft, `PRAGMA table_info` plus one additive `ALTER TABLE` adds the marker.
- Old binaries ignore the extra table, so code rollback does not require a DB
  downgrade.
- No backfill from `messages` is performed: it cannot prove the last admitted
  work completed quiescently. Empty table means automatic compact off until new
  successful activity.

### Configuration

- Remove `AUTO_COMPACT_PCT` from code and `.env.example`; an old production
  `.env` entry becomes a harmless unused variable for one rollback-compatible
  release.
- Fixed constants are `AUTO_COMPACT_TZ="Asia/Krasnoyarsk"`,
  `AUTO_COMPACT_WINDOW_START=time(23, 0)`,
  `AUTO_COMPACT_WINDOW_END=time(8, 0)`,
  `AUTO_COMPACT_IDLE=timedelta(minutes=55)`, and
  `AUTO_COMPACT_MIN_CONTEXT_PCT=20.0`. They cannot be overridden into daytime.
- Update `system_prompt.txt` to state night/offline-only automatic behavior and
  manual `/compact`; remove the obsolete immediate-95% statement.

### Session and Telegram behavior

- Do not change the session file format.
- Do not change task #13's `_SessionReplacement`, commit point, cancellation,
  limit latch, or progress terminalization.
- Manual `/compact` remains time-independent.
- New strings are additive in Russian and English.

## Compact prompt evaluation

### Files

- `tests/fixtures/compact_summary_cases.json` — ten synthetic fixture
  definitions:
  decision reversal; durable preference vs one-off; exact file states; command
  evidence; pending blocker; temporal state; recent correction; large-output
  pressure; unresolved conflict; secret/idempotent pre-save.
- `tests/compact_summary_scorer.py` — pure deterministic scorer shared by tests
  and the live runner.
- `tests/test_compact_prompt.py` — schema/scorer unit tests, prompt section
  validator/redactor tests, and malformed-summary SID-preservation tests.
- `scripts/evaluate_compact_prompt.py` — isolated live runner; no production
  session IDs or cog-second-brain files.

### Deterministic score

Each fixture declares:

- required section headers and exact atomic anchors;
- exact paths, commands, exit codes, numbers, pending actions, timestamps;
- forbidden decoy claims and raw fake-secret strings;
- expected redacted recent-message text;
- seeded temporary files and exact expected post-run file state/diff;
- a source fact ledger for the final claim audit.

The scorer fails a run on any:

- missing/duplicated/out-of-order section;
- missing critical anchor or altered exact path/command/number;
- raw secret or forbidden decoy;
- recent-message mismatch after newline normalization and specified redaction;
- unexpected/duplicated file write or unrelated-file change.

It emits per-category booleans and a single `passed` value; no weighted average
can hide a hard failure.

### Live promotion gate

Run three independent live generations for every fixture: **30/30 required**.
Each run uses:

- a fresh temporary working directory and session;
- only synthetic fixture content and fake secrets;
- the target `claude-opus-5` model;
- the installed target SDK/CLI;
- the exact production `COMPACT_PROMPT`;
- file tools confined to the temporary fixture tree.

The three `large-output pressure` runs are not ordinary short prompts: each
must first reproduce the target runtime's hard-context rejection and then run
the explicit manual native-boundary → custom transactional retry path. Thus
the required 30 runs include three real recovery generations rather than
claiming recovery from a mocked event sequence alone.

Evidence artifact records timestamp, model, SDK/CLI versions, fixture/run IDs,
summary hash, deterministic category results, file-diff hash, and failure
reason. Successful redacted summaries may be retained; a failed output that
contains a fake secret stays outside Git and is represented by hashes/results.

After deterministic scoring, every output is checked line-by-line against the
fixture fact ledger for zero unsupported claims and no transcript dumping.
This source-ledger audit is also a hard gate because an open-world fabrication
cannot be proven absent by substring scoring alone.

Promotion requires:

- 30/30 deterministic passes;
- 30/30 source-ledger audits with zero fabrication;
- zero raw secrets;
- zero non-idempotent/unrelated file changes.

If OAuth quota/rate limit interrupts the 30 runs, record completed run IDs and
the normalized blocker and mark evaluation **INCOMPLETE/FAILED**. This always
blocks service promotion. If the exact target SDK/CLI is unavailable before
merge, the code may be merged only with explicit orchestrator approval and the
gate remains visibly pending for T4; the production process is not restarted
until the isolated target-runtime run completes. Resume after quota reset from
only the missing independent runs. Do not reduce three runs, remove a fixture,
or weaken any threshold.

## Tests and verification

### Narrow tests per ticket

1. T1:
   `pytest -q tests/test_claude_session_limit.py tests/test_response_limit.py`
2. T2:
   `pytest -q tests/test_chat_activity.py tests/test_preventive_compact.py`
3. T3:
   `pytest -q tests/test_compact_prompt.py tests/test_compact_limit.py`

Then:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q
```

The known local index issue resolving `claude-agent-sdk>=0.2.128` is not
papered over. Use the already-working project environment or production target
package for live gates; dependency-resolution failure is reported separately,
not converted into a skipped test.

### Required race/boundary coverage

- 22:59:59, 08:00:00, noon, and every tested context value
  (20/95/100%) produce zero automatic compact;
- 23:00:00 and 07:59:59 are eligible only after 55 minutes and all other gates;
- accepted activity before final reservation wins and reschedules;
- no compact in every non-IDLE phase or with pending/deferred/transcription
  work;
- manual request during processing remains sticky and wins over scheduler
  wake-up;
- timer fired in daytime survives to the next night;
- restart from `quiescent=0`, missing row, or unknown context fails closed;
- restart from `quiescent=1`, inactivity >=55, and context >=20 makes exactly
  one night attempt;
- failed admission write admits nothing; failed finish write stays
  non-quiescent after a new `MessageLog` instance;
- media admission is durable before the first download/transcription await;
  a crash there reopens as non-quiescent, while ordinary exceptions finish the
  media lifecycle in `finally`;
- auto compact limit/error has no same-episode retry and preserves task #13
  SID/progress guarantees;
- low/unknown/limit/error/success/cancellation and crash after durable claim do
  not repeat after restart until a new activity clears the episode marker;
- restart/night probing connects the old SID without permitting invalidation;
  unknown/zero/failure releases the reservation without compact;
- manual `/compact` during the reserved probe with context `<20%` flips sticky
  provenance and executes exactly one manual transaction;
- unsolicited/automatic native `compact_boundary` is observable as an
  invariant violation; an explicitly awaited manual recovery boundary is not;
- hard-context errors produce one friendly outcome in both user-message and
  reminder paths, without raw text, retry, `empty`, or `📋`.
- simulated hard limit → explicit manual `/compact` → native manual boundary →
  retried validated handoff commits a new SID; missing boundary/error/limit
  drains the terminal Result and preserves the durable SID.

## Rollback-safe deployment order

Deployment is a post-merge gate; Phase 3 implementation must not touch
production before merge approval.

1. Record production HEAD, service state, `.env`, package versions, existing
   dirty diff/stash, `messages.db` plus WAL/SHM backup, and SHA-256 hashes of all
   session files.
2. Preserve/reapply existing dirty production changes; do not overwrite the
   known adaptive-thinking patch or unrelated state.
3. Pull the merged release while the old process remains running.
4. Verify exact target dependencies before any restart:
   `claude-agent-sdk==0.2.128`, bundled Claude Code `2.1.220`. A mismatch blocks
   promotion; do not opportunistically upgrade.
5. Run import/compile and the deterministic test suite against the pulled
   checkout.
6. Run the isolated 30-generation prompt gate from a temporary CWD and
   temporary session files. A quota/rate-limit/incomplete/failing result blocks
   restart and restores the checkout to the recorded HEAD.
7. Verify the three target-runtime `large-output pressure` evidence records:
   `/compact` was advertised in `system/init`, a normal prompt first returned
   the recorded hard-context variant, bot recovery observed
   `compact_boundary(trigger="manual")`, and the retried validated handoff
   committed a different candidate SID. Each record includes SDK/CLI/model,
   pre-boundary tokens, normalized error, boundary metadata, old/new temporary
   SID hashes, and result. Quota/rate/cost/incomplete status blocks promotion;
   do not substitute the user's session or weaken this gate.
8. Run an isolated OAuth SDK control smoke with `DISABLE_AUTO_COMPACT=1`;
   require `get_context_usage()["isAutoCompactEnabled"] is False`. Use no bot
   session file and no cog-second-brain content.
9. Only after all gates pass, perform one controlled
   `systemctl restart kesha-bot-vps`.
10. Verify `active/running`, startup model `claude-opus-5`, no traceback/SQLite
   migration error, additive `chat_activity` schema, and session-file hashes
   unchanged.
11. Run one isolated normal OAuth request and resume using temporary session
    state. Do not force real compact/quota on the user's active session.
12. Verify fixed-window configuration through a quota-free production-checkout
    boundary smoke and inspect logs for zero unexpected `compact_boundary`.

Rollback on any post-restart failure:

1. restore the recorded code HEAD, `.env`, venv/package state if changed, and
   pre-existing dirty patch;
2. leave the additive `chat_activity` table in place (old code ignores it);
   restore the DB backup only if migration integrity itself failed;
3. restart the old service and verify `active/running`;
4. verify all original session hashes;
5. report the exact failed gate. Never keep the new service running with native
   auto-compaction enabled or an incomplete prompt evaluation.

## Non-goals

- No generic scheduler/state-machine framework.
- No new scheduler database or queue.
- No transcript parser or automatic backfill.
- No RAG, reminder, media, or response-stream refactor.
- No Messages API migration.
- No change to model, OAuth, proxy, task #13 session transaction, or manual
  compact semantics.

## Tickets

### T1 — Own compaction at the SDK boundary and recover cleanly at daytime hard limit

- Files:
  `claude_session.py`, `response_stream.py`, `config.py`,
  `tests/test_claude_session_limit.py`, `tests/test_response_limit.py`
- Vertical outcome:
  every Claude subprocess has native auto-compaction disabled, native boundary
  violations are observable, and a daytime full context ends in one friendly
  manual-`/compact` Telegram outcome without retry/reset.
- AC:
  - `_make_options().env["DISABLE_AUTO_COMPACT"] == "1"` without replacing
    unrelated inherited environment;
  - official hard-context Result/exception/partial-stream variants normalize to
    terminal `kind="context_limit"` and never call reconnect/reset;
  - user and reminder Telegram paths replace raw partial text with exactly one
    localized `/compact` instruction; no raw stack/error, `empty`, or `📋`;
  - unsolicited/automatic native `compact_boundary` is logged as an invariant
    violation; `run_native_manual_compact()` accepts only an explicitly awaited
    manual boundary plus successful terminal Result and fully drains failures;
  - existing task #13 usage-limit tests remain green.
- blocked-by: none

### T2 — Persist activity and run exactly one restart-safe night scheduler

- Files:
  `message_log.py`, `chat_state.py`, `claude_session.py`, `handlers.py`,
  `inbox_server.py`, `bot.py`, `config.py`, `.env.example`,
  `system_prompt.txt`, `compact.py`, `tests/test_chat_activity.py`,
  `tests/test_claude_session_limit.py`, `tests/test_preventive_compact.py`
- Vertical outcome:
  every admitted conversation cycle is durably tracked; one per-chat scheduler
  is the only automatic owner and can compact only under the accepted
  night/offline contract across activity, races, and restart.
- AC:
  - additive `chat_activity` migration preserves all existing messages/session
    files and old code can ignore the table;
  - admission commits `quiescent=0` before state mutation; failure admits
    nothing and returns one safe retry outcome;
  - all async media handlers persist non-quiescence before their first
    download/transcription await; crash survives as false, while caught
    failures balance the media lifecycle;
  - only drained `IDLE` commits `quiescent=1`; crash/write failure remains
    false after reopening SQLite;
  - old 95% and preventive execution paths plus
    `compact.maybe_auto_compact()` are removed; no duplicate owner remains;
  - at 20/95/100% context, every time outside `[23:00,08:00)` produces zero
    automatic compacts;
  - all phase/pending/media/activity/manual races satisfy the boundary suite;
  - daytime timer reschedules to night; restart behavior follows durable
    inactivity/quiescence and a fresh resumed context probe; missing/unknown
    fails closed without invalidating the SID;
  - a claimed low/unknown/limit/error/success/cancelled attempt remains
    disarmed across restart for the same `last_activity_utc`; the next admitted
    activity clears the claim;
  - manual `/compact` bypasses time/inactivity and wins provenance races;
    specifically, manual during a reserved `<20%` probe still executes once;
  - usage-limit failure produces one attempt and preserves task #13
    transaction/progress behavior;
  - `/clear` and successful compact leave no stale automatic request.
- blocked-by: T1

### T3 — Promote a secret-safe, fixture-proven handoff prompt

- Files:
  `compact.py`, `claude_session.py`, `chat_state.py`,
  `tests/fixtures/compact_summary_cases.json`,
  `tests/compact_summary_scorer.py`,
  `tests/test_compact_prompt.py`,
  `scripts/evaluate_compact_prompt.py`,
  `docs/tasks/14/compact-eval.json`,
  `docs/tasks/14/report.md`
- Vertical outcome:
  successful compact commits only a structurally valid, globally
  secret-redacted handoff whose preservation quality passed the accepted
  deterministic and live gates; failures leave the old SID resumable.
- AC:
  - accepted prompt sections/security/idempotent pre-save contract is present;
  - high-confidence secrets are redacted before validation, accounting,
    logging, preamble, or Telegram; captured logs contain no seeded raw secret;
  - missing/out-of-order top-level sections fail before candidate start and
    preserve the original SID/progress terminalization; section-like text in
    `RECENT VERBATIM` is treated as payload;
  - manual custom-summary `context_limit` invokes one native
    `/compact <accepted instructions>`, requires a manual boundary, retries the
    task #13 validated transaction once, and commits a new SID on success;
    automatic compact never uses the native fallback;
  - native failure preserves the old SID; native success plus retry failure
    preserves the same compacted SID, reports one explicit partial outcome, and
    does not clear/retry;
  - all ten deterministic fixtures enforce exact anchors, forbidden claims,
    redacted recent messages, and exact/idempotent file diffs;
  - three independent live generations per fixture complete: 30/30
    deterministic passes, 30/30 source-ledger audits, zero fabrication, zero
    raw secrets, zero unrelated/duplicate writes;
  - incomplete/rate-limited live evaluation is recorded as failed and blocks
    service promotion; if target-only execution is needed, T4 completes the
    unchanged gate before restart;
  - focused and full pytest pass, and task #13 compact transaction tests remain
    unchanged/green.
- blocked-by: T2

### T4 — Execute rollback-safe promotion and verify production invariants

- Files:
  `docs/tasks/14/report.md` plus production state only after merge approval
- Vertical outcome:
  the already-merged implementation is promoted only after package, prompt,
  native-disable, service, OAuth, session-hash, and rollback gates pass.
- AC:
  - pre-deploy snapshot preserves HEAD, `.env`, dirty diff, DB/WAL/SHM,
    package versions, service state, and session hashes;
  - production SDK/CLI are exactly 0.2.128/2.1.220;
  - isolated context usage reports `isAutoCompactEnabled=false`;
  - prompt live gate is complete before restart;
  - target-runtime hard-context recovery records the rejection, manual
    boundary, and successful candidate SID replacement using only temporary
    state;
  - service is `active/running`, logs `claude-opus-5`, and has no startup,
    migration, or invariant error;
  - isolated OAuth request/resume succeeds without touching user sessions;
  - session hashes are unchanged;
  - any failure restores the prior code/service and reports the exact blocker.
- blocked-by: T3
