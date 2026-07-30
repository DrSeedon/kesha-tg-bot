# Task #14 — Phase 2 plan: night-only automatic compact

**Status:** revised plan after live native-recovery falsification; production
unchanged, implementation delta awaits approval
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
time and retain task #13's transactional SID/limit behavior. A measured
absolute daytime admission reserve prevents ordinary turns from consuming the
headroom needed by that custom transaction.

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
6. **The supported recovery is prevention, not SDK slash commands.** Exact
   runtime measurement found no `compact_boundary(trigger="manual")` after
   Agent SDK `query("/compact ...")`. `run_native_manual_compact` is removed.
   The measured prompt delta is 1,622 tokens and declared maximum output is
   64,000; the manual floor is 80,000 remaining tokens and normal-turn
   admission requires 208,000 plus the assembled input's UTF-8 byte length.

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
- `/clear` and any successful custom compact re-arm/disarm from the next real
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

### SDK ownership and daytime admission reserve

`ClaudeSession._make_options()` supplies:

```python
options.env = {"DISABLE_AUTO_COMPACT": "1"}
```

The SDK merges this with the inherited process environment; proxy/auth values
are not copied or hardcoded.

`ClaudeSession.send_message()` gains two narrow normalizations:

- An unsolicited `SystemMessage(subtype="compact_boundary", trigger="auto")`
  logs a critical invariant violation with trigger metadata.
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

`ClaudeSession.run_native_manual_compact()` is deleted. The exact target
runtime advertised `compact` but produced no manual boundary when the string
was submitted through Agent SDK; a terminal Result alone is not proof that
context was reduced.

Before every non-command LLM batch, `ChatState._run_batch()` assembles the
exact prompt and calls the flat
`ClaudeSession.check_context_reserve(combined)` helper:

```python
await self._ensure_connected(preserve_session=True)
usage = await self._client.get_context_usage()  # uncached control response
remaining = usage["maxTokens"] - usage["totalTokens"]
required = 208_000 + len(combined.encode("utf-8"))
```

The code shape does not let `get_context_usage()` erase the reason for a failed
resume. `check_context_reserve()` first calls
`_ensure_connected(preserve_session=True)` itself:

- `No conversation found` returns typed `session_unavailable`, preserves the
  in-memory and durable SID, performs zero query, and produces one static
  `/clear` instruction;
- other connect/control failures return transient `unknown`, preserve SID,
  perform zero query, and produce one retry-later outcome without latching;
- only after a successful preserved connection does it read current usage
  directly from the client. It never uses `ClaudeSession._last_ctx_usage` or
  the compatibility fallback in `get_context_usage()`.

Thus a stale SID cannot loop forever behind the reserve turnstile, while a
temporary usage failure is not mislabeled as disposable session state.

The constants come from three exact-runtime measurements:

```text
COMPACT_PROMPT input delta = 1,622 tokens (3/3)
model maxOutputTokens      = 64,000
manual compact floor       = round_up((1,622 + 64,000) * 1.20) = 80,000
normal-turn envelope       = 64,000 model + 64,000 agent/tool
admission base             = 208,000
```

The same helper is called again in `response_stream._ask_inner()` immediately
before every actual retry-query after a timeout/session/process failure. A
retry that no longer has authoritative headroom ends with the static
`/compact`-then-resend outcome and performs no second query. The first attempt
remains visible in the session transcript; it is never hidden by silently
spending the compact floor on another attempt.

`ClaudeSession` itself is not allowed to own a hidden retry:

- `send_message()` connects with `preserve_session=True`;
- the existing `No conversation found` / broad `exit code 1` branch no longer
  calls `_invalidate_session()` and no longer recursively calls
  `send_message(text)`;
- it yields one typed terminal `session_unavailable` error with the same durable
  SID, and `response_stream` tells the user once that only explicit `/clear`
  can start over;
- no internal path issues a second query. Every permitted retry remains visible
  to the guarded `response_stream` owner.

This intentionally trades automatic recovery from a stale SID for explicit,
non-destructive recovery. A generic exit code is not evidence that the
conversation is disposable.

The byte length is a safe upper bound for the assembled input token count and
avoids a second tokenizer dependency. The guard requires the exact numeric
fields, matching `maxTokens/rawMaxTokens`, positive totals, and
`isAutoCompactEnabled is False`; missing/malformed/failed usage fails closed
for that batch. It also requires model `claude-opus-5[1m]`. The measured
`64_000` is a named invariant, not a free magic value:

- deployment runs one isolated exact-runtime Result and requires
  `model_usage["claude-opus-5[1m]"]["maxOutputTokens"] == 64_000`;
- `ClaudeSession.send_message()` stores the same field from every later
  terminal Result; any observed mismatch makes subsequent reserve checks
  unknown/fail-closed;
- a new/cleared session has no Result yet, so it may use the deployment-verified
  constant only while `get_context_usage()` reports the exact expected model,
  1M max, and auto-compact disabled.

If `remaining < required`, the batch never reaches `session.query()`. It
receives one static localized terminal message instructing the user to run
`/compact` and then resend. The batch is not retained only in volatile memory:
the explicit terminal outcome is the retry contract across restart. A
`ChatState._context_reserve_blocked` then prevents repeated context probes
until any successful custom compact or `/clear`; subsequent rejected batches
still receive their one terminal outcome. The latch is set only by a confirmed
numeric reserve breach or an observed terminal `context_limit`. A transient
unknown/malformed usage response sends one static “could not verify, retry”
outcome but does not latch, so a later batch can recover without unnecessary
compact.

Entries arriving during `PROCESSING` are deferred instead of injected. After
the current Result they form a new batch and cross the same authoritative
preflight boundary. This removes the usage-snapshot/injection race without a
new scheduler or state machine.

Manual `/compact` bypasses normal-turn admission and remains exactly task #13's
custom summary → validated handoff → candidate session → atomic SID commit.
It must start while at least the 80,000-token floor remains. A legacy session
already below that floor, unknown usage, limit, or hard-context error preserves
the SID and ends with one explicit limitation; it never resets, retries, or
accepts a fake native compact.

Exact-runtime measurement already showed that a newly connected session with
`session_id is None` returns a nonzero authoritative usage snapshot before its
first query (`totalTokens` 9,428–9,530 in the three reserve runs). `/clear →
first message` therefore follows the normal guard. A zero/None response rejects
that single batch without setting the reserve latch; a later message probes
again and cannot become permanently blocked by an empty cache.

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
- Plain bot commands that do not invoke Claude bypass the admission guard.
- Any successful custom compact (manual or night automatic) and `/clear` clear
  the reserve latch; failed compact leaves it set.
- New strings are additive in Russian and English.

## Compact prompt evaluation

### Files

- `tests/fixtures/compact_summary_cases.json` — ten synthetic fixture
  definitions:
  decision reversal; durable preference vs one-off; exact file states; command
  evidence; pending blocker; temporal state; recent correction;
  reserve/admission/manual recovery; unresolved conflict; secret/idempotent
  pre-save.
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

The three reserve-recovery runs use a real temporary SDK session and the exact
target usage response shape. Each proves that a synthetic below-threshold
normal batch performs zero query/SID mutation, then invokes the real custom
manual compact while measured headroom remains, validates the handoff, commits
a new temporary SID, and resumes one post-compact control turn. No native slash
command or hard-context inflation is used.

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

The failed v1 checkpoint remains immutable evidence of the falsified
architecture. The revised run uses seed `task-14-compact-v2` and a new v2
artifact; no completed v1 cell is overwritten or counted.

If OAuth quota/rate limit interrupts the 30 runs, record completed run IDs and
the normalized blocker and mark evaluation **INCOMPLETE/FAILED**. Only 529 or
explicit overload receives bounded exponential backoff with deterministic
case/run identity; retry attempts never count as independent samples. Resume
only missing/incomplete v2 cells. This always blocks promotion. Do not reduce
three runs, remove a fixture, or weaken any threshold.

## Tests and verification

### Narrow tests per ticket

1. T1:
   `pytest -q tests/test_claude_session_limit.py tests/test_response_limit.py
   tests/test_preventive_compact.py`
2. T2:
   `pytest -q tests/test_chat_activity.py tests/test_preventive_compact.py`
3. T3:
   `pytest -q tests/test_compact_prompt.py tests/test_compact_limit.py
   tests/test_compact_evaluator.py`

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
  invariant violation;
- hard-context errors produce one friendly outcome in both user-message and
  reminder paths, without raw text, retry, `empty`, or `📋`.
- usage at `remaining == required` admits exactly once; one token below rejects
  before user logging/Claude query and preserves SID/context;
- every timeout/session/process retry rechecks authoritative remaining context;
  below-threshold/unknown retry performs zero second query and emits one static
  terminal outcome;
- `No conversation found` and broad `exit code 1` perform zero recursive query,
  zero SID-file mutation, and one typed terminal outcome; only explicit
  `/clear` may discard that SID;
- missing/malformed/zero usage fails closed; a latched near-full session is not
  probed repeatedly until any successful custom compact or `/clear`;
- previous valid `_last_ctx_usage` plus a fresh zero control response rejects
  the batch with zero query; cached usage can never authorize admission;
- stale-SID failure during the preflight resume returns one typed `/clear`
  terminal, zero query, and unchanged SID; it is distinct from transient
  unknown usage;
- `/clear → first message` with a fresh nonzero target-runtime snapshot admits;
  an initial zero/None snapshot rejects once without latching and a later valid
  snapshot admits;
- processing-time arrivals are deferred and independently preflighted after the
  current Result; no injection races the usage snapshot;
- rejected user/reminder/inbox/media batches each receive one static terminal
  retry contract and are not claimed as processed by Claude;
- manual `/compact` bypasses normal admission, succeeds at the measured floor,
  commits a new SID, clears the latch, and never calls native `/compact`;
- a successful night automatic custom compact also clears a daytime reserve
  latch;
- deployment and terminal Result parsing require measured
  `maxOutputTokens == 64_000`; mismatch fails subsequent admission closed;
- a legacy already-hard-full session returns one explicit limitation, preserves
  SID/session file, and performs no reset/retry/native slash acceptance.

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
7. Verify the three target-runtime reserve-recovery records: exact
   `totalTokens/maxTokens/rawMaxTokens/maxOutputTokens`, rejected normal-turn
   query count zero, unchanged pre-compact temporary SID hash, successful
   custom handoff validation/commit, changed candidate SID hash, and successful
   post-compact control turn. Quota/rate/cost/incomplete status blocks
   promotion; do not substitute the user's session or weaken this gate.
8. Run an isolated OAuth SDK control smoke with `DISABLE_AUTO_COMPACT=1`;
   require `get_context_usage()["isAutoCompactEnabled"] is False`, exact
   `maxTokens/rawMaxTokens/model`, and terminal
   `model_usage.maxOutputTokens == 64_000`. Use no bot session file and no
   cog-second-brain content.
9. Only after all gates pass, perform one controlled
   `systemctl restart kesha-bot-vps`.
10. Verify `active/running`, startup model `claude-opus-5`, no traceback/SQLite
   migration error, additive `chat_activity` schema, and session-file hashes
   unchanged.
11. Run one isolated normal OAuth request and resume using temporary session
    state. Do not force real compact/quota on the user's active session.
12. Verify fixed-window and reserve configuration through quota-free
    production-checkout boundary tests and inspect logs for zero unexpected
    `compact_boundary`.

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
- No PTY/subprocess automation of interactive Claude Code slash commands.

## Tickets

### T1 — Own compaction and preserve daytime custom-summary headroom

- Files:
  `claude_session.py`, `chat_state.py`, `response_stream.py`, `config.py`,
  `tests/test_claude_session_limit.py`, `tests/test_preventive_compact.py`,
  `tests/test_response_limit.py`
- Vertical outcome:
  every subprocess has native auto-compaction disabled; every normal LLM batch
  is authoritatively preflighted while enough custom-summary headroom remains;
  insufficient/unknown headroom ends once without query/reset.
- AC:
  - `_make_options().env["DISABLE_AUTO_COMPACT"] == "1"` without replacing
    unrelated inherited environment;
  - official hard-context Result/exception/partial-stream variants normalize to
    terminal `kind="context_limit"` and never call reconnect/reset;
  - user and reminder Telegram paths replace raw partial text with exactly one
    localized `/compact` instruction; no raw stack/error, `empty`, or `📋`;
  - unsolicited/automatic native `compact_boundary` is logged as an invariant
    violation; `run_native_manual_compact()` does not exist;
  - authoritative usage exposes matching positive max/current values and
    `isAutoCompactEnabled=false`; malformed/failed usage rejects before query;
  - reserve admission reads the uncached client control response; a prior valid
    cache plus current zero rejects with zero query;
  - preflight distinguishes stale resume from transient unknown:
    `No conversation found` gives one `/clear` terminal, zero query, and
    unchanged SID/session file;
  - admission uses `208_000 + len(combined.encode("utf-8"))`, with exact
    threshold/below-threshold tests and unchanged SID/context;
  - every actual retry-query repeats the reserve helper; failure performs zero
    retry query and terminalizes once;
  - `_ensure_connected`/`send_message` preserve an existing SID and never
    invalidate/recursively retry on `No conversation found` or `exit code 1`;
    tests assert one query and unchanged durable SID;
  - entries during processing are deferred rather than injected, then
    independently preflighted; no message disappears without a terminal retry
    instruction;
  - confirmed reserve breach/context-limit latches without repeated probes and
    is cleared by any successful custom compact or `/clear`; unknown usage
    fails one batch without latching; `/clear → first message` is covered on
    the exact runtime; plain commands remain available;
  - exact model/max/raw/auto invariants and measured
    `maxOutputTokens == 64_000` are required; a later mismatch fails closed;
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
  `docs/tasks/14/compact-eval.json` (immutable failed v1),
  `docs/tasks/14/compact-eval-v2.json` (revised promotion gate),
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
  - manual custom summary is the only compact implementation; context
    limit/usage/error preserves the old SID and never invokes a native slash
    command;
  - all ten deterministic fixtures enforce exact anchors, forbidden claims,
    redacted recent messages, and exact/idempotent file diffs;
  - the former hard-context fixture is replaced by
    reserve-reject → custom-manual-compact → candidate-resume recovery;
    3/3 records prove zero rejected query and successful transactional resume;
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
  native-disable, reserve, service, OAuth, session-hash, and rollback gates
  pass.
- AC:
  - pre-deploy snapshot preserves HEAD, `.env`, dirty diff, DB/WAL/SHM,
    package versions, service state, and session hashes;
  - production SDK/CLI are exactly 0.2.128/2.1.220;
  - isolated context usage reports `isAutoCompactEnabled=false`;
  - isolated terminal model usage reports `maxOutputTokens=64_000`;
  - prompt live gate is complete before restart;
  - target-runtime reserve recovery records zero rejected normal query and
    successful custom candidate SID replacement using only temporary state;
  - service is `active/running`, logs `claude-opus-5`, and has no startup,
    migration, or invariant error;
  - isolated OAuth request/resume succeeds without touching user sessions;
  - session hashes are unchanged;
  - any failure restores the prior code/service and reports the exact blocker.
- blocked-by: T3
