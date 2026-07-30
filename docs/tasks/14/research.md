# Task #14 — Night-only automatic compact and high-fidelity handoff

**Date:** 2026-07-30
**Scope:** research only; no code or production changes

## Question

### Context

Kesha is a single-user Telegram bot backed by one persistent
`ClaudeSDKClient`. It currently has two application-level automatic compact
triggers and also leaves Claude Code's native near-limit auto-compactor enabled.
Custom compaction asks the active model for a summary, starts a fresh Claude
session, seeds it with that summary, and atomically replaces the persisted
session ID.

### Change under test

Automatic compaction must never happen during the user's daytime conversation.
It may happen only in a Krasnoyarsk night window and only after evidence that
the user is offline. Manual `/compact` must remain available. The handoff prompt
must preserve the state needed to continue without fabricating facts or copying
an entire transcript.

### Baseline and measurable outcome

The baseline is:

1. immediate application compact after any completed response at
   `AUTO_COMPACT_PCT` (default 95%);
2. preventive compact after 55 minutes of inactivity at context >=20%;
3. native Claude Code auto-compaction near the model limit.

The change is successful only if all automatic owners are blocked outside the
night/offline policy, no compact races active work, restart does not lose a
pending night attempt or create a retry loop, manual compact still works, and
summary fixtures retain all critical anchors with zero unsupported claims.

## Hypotheses considered

### H1 — Fixed night window alone is sufficient

**Claim:** `23:00–08:00 Asia/Krasnoyarsk` alone identifies offline periods.

**Falsifier:** any user/reminder activity or active Claude turn inside the
window at compact time.

**Result:** **REFUTED.** The state machine explicitly supports user injection
during `PROCESSING`, so time-of-day alone can compact an active night
conversation.

### H2 — Inactivity alone is sufficient

**Claim:** the existing 55-minute inactivity timer identifies a safe compact
point regardless of clock time.

**Falsifier:** measured daytime returns after such pauses, or the explicit
requirement that daytime auto-compact never run.

**Result:** **REFUTED.** The historical artifact contains 39 potential
55-minute compacts during 12:00–18:00 and 38 during 18:00–23:00. More
importantly, inactivity alone cannot satisfy the hard daytime prohibition.[1]

### H3 — Window AND inactivity, with one automatic owner

**Claim:** a fixed night window plus a durable inactivity test and an atomic
idle-state recheck prevents conversational interruption, while disabling the
CLI auto-compactor makes the policy complete.

**Falsifier:** any automatic compact outside the window, during a non-idle
phase, immediately after restart without durable inactivity evidence, or from a
native `compact_boundary`.

**Result:** **CONFIRMED as the simplest viable design.** The controls exist in
the current CLI, the required state is already centralized in `ChatState`, and
the message log already persists timestamps across bot restarts.[2][3]

### H4 — Keep a daytime emergency compact at 95%

**Claim:** a very high context threshold justifies overriding the daytime rule.

**Falsifier:** the stated requirement that no daytime automatic compact run at
any threshold.

**Result:** **REFUTED by requirement.** Disabling the safety net means a
daytime turn can instead reach `Prompt is too long`; official Claude Code
documentation explicitly names this tradeoff and recommends manual
`/compact`.[4] That failure must be presented clearly, not silently converted
into a daytime compact.

## Current Kesha behavior

### Trigger and state audit

| Path | Current behavior | Consequence |
|---|---|---|
| `ChatState._maybe_auto_compact()` | Runs after every completed response; at `pct >= AUTO_COMPACT_PCT` it changes `PROCESSING -> COMPACTING` and compacts immediately. | Can run at noon and before deferred messages are drained. |
| `ChatState._on_preventive_elapsed()` | Every incoming entry arms a 55-minute task. At context >=20%, it calls `request_compact(automatic=True)` at any hour. The local hour is logged but not used as a gate. | Can interrupt a daytime thread after a pause. |
| `ClaudeSession._make_options()` | Does not set `DISABLE_AUTO_COMPACT`. | Claude Code may independently compact near the model limit even if both Python paths gain a night check. |
| `request_compact()` | Manual and automatic requests share the transaction. A request during `PROCESSING` is deferred; manual provenance is sticky. An immediate request can move pending messages to `deferred` and cancel pending transcriptions. | Manual semantics are correct, but an automatic race must never enter this path while daytime/busy. |
| `accept_entry()` | A user message during `PROCESSING` calls `session.inject()`; a message during `COMPACTING` is deferred. Any entry resets the preventive timer. | A pre-check outside the state lock is insufficient: activity can start after the check. |
| `ChatRegistry.get()` | Creates `ChatState` lazily. Preventive timers are in-memory only and start only after an entry. | After restart, a high-context persisted session has no scheduled night check until another event creates/arms its state. |
| `MessageLog` / `get_context_usage()` | A normal message is logged only when `_run_batch()` begins; an injected message is logged after successful injection. `_last_ctx_usage` is memory-only, and `get_context_usage()` does not connect a resumed-but-disconnected session. | `MAX(messages.timestamp)` can lag a just-accepted entry across a crash, and merely pre-creating `ChatState` after restart still cannot know current context usage. |
| `compact_session()` | Task #13 protects the old durable SID before summary, rejects limit/error/empty summaries, rolls back on cancellation, and commits only after a valid continuation turn. | Session safety is now strong, but summary omissions after a successful commit are still semantic data loss. |

Code evidence: `chat_state.py:118-154,256-293,419-457,541-573,608-674`,
`claude_session.py:214-232`, `compact.py:48-196`,
`handlers.py:172-181`, and `bot.py:132-151,230-246`.

### Exact races and disruption mechanisms

1. **Threshold race with deferred user input.** `_maybe_auto_compact()` runs
   inside `_run_batch()` before `_finish_processing()` drains `deferred`.
   Messages arriving during compact are safe from deletion but wait behind a
   visible `🗜` operation and a summary turn.
2. **Preventive check-to-use race.** `_on_preventive_elapsed()` reads
   `is_busy` without the lock and then awaits `request_compact()`. A user turn
   can begin between those operations. Current `request_compact()` then queues
   the automatic compact after that turn instead of cancelling it.
3. **Native-owner escape.** A Python night gate cannot stop Claude Code's own
   compactor. The SDK emits `SystemMessage(subtype="compact_boundary")` after
   native compaction.[5]
4. **Timer disappearance.** If the 55-minute task fires while busy, context is
   below 20%, usage lookup fails, or the service restarts, the task returns and
   no night-boundary retry exists until a new entry re-arms it.
5. **Two automatic implementations.** The 20% preventive path and 95%
   post-response path independently decide when to compact. Their duplicated
   policy is the root cause of inconsistent timing and makes a hard guarantee
   difficult.

### What is forgotten after a successful custom compact

Kesha's custom compact is a new session seeded only with `COMPACT_PROMPT`'s
output. The bot atomically changes its sole durable session pointer to the new
SID. The full message history remains separately in `storage/messages.db` and
Claude Code keeps transcripts on disk, but neither is automatically injected
into the new active context. `search_memory` helps only if the model decides to
call it and retrieval finds the omitted fact.

Therefore these are distinct loss mechanisms:

- an early instruction omitted from the summary is no longer active;
- an exact path, command, exit code, number, or latest temporal state can be
  generalized into a useless paraphrase;
- an unresolved conflict can be collapsed into a false decision;
- an explicit user preference can be confused with a one-off instruction;
- pending work can lose its blocker, owner, or next executable action;
- recent wording can be lost even though it is the most likely context for the
  next message;
- nested/path-scoped instructions are not necessarily re-injected until a
  matching file is read again.[6]

Anthropic documents the same semantic risk: older history is replaced with a
summary, and detailed early instructions may be lost.[7][8]

## Recovered Kesha measurements

The underlying preserved artifact is
`artifacts/cache-compact-analysis.html`, introduced in commit `0c30fe2` and
corrected in `3e056bf` and `a75ffc5`. Its companion scripts are under
`docs/tasks/cache-compact/`.[1]

### Measured values in the artifact

| Measurement | Recorded result |
|---|---:|
| All message gaps in timing sweep | 1,211 |
| `P(gap>60 | gap>55)` | 92.5% |
| False compact at 55 minutes | 7.5% |
| Potential compacts at gaps >55 minutes | 119 |
| Assistant was last speaker | 108/119 (91%) |
| User was last speaker | 11/119 (9%) |
| Night segment | 23:00–08:00, `n=93` |
| Night `P(gap>60 | gap>30)` | 84% |
| Night `P(gap>60 | gap>50)` | 100% |
| Day 09:00–18:00, `n=721`, `P(gap>60 | gap>30)` | 52% |
| Measured summary size used in economics | 9,120 chars, estimated 2,280 tokens |
| Measured post-compact context cited by current code | about 4% |
| Calculated context breakeven | 19.4%; implementation gate 20% |

The approximate Wilson 95% intervals reconstructed from the rounded segment
percentages are 75–90% for the night 84% result and 48–56% for the daytime 52%
result. These intervals are illustrative only because the artifact does not
retain exact per-segment numerators.

### Important limitation and counter-evidence

The raw `messages.db` snapshot and the extraction script that produced the
119/1,211 gap dataset are not present in this worktree. The HTML is the
underlying retained artifact, but a fresh reproduction from raw messages is not
possible here.

The artifact's conclusion “91% lose zero context” is also not a summary-quality
measurement. “Assistant spoke last” is a proxy for conversational closure, not
proof that every fact needed next morning survived. Likewise, “after two hours
the detail is not needed” is an unsupported assumption, and RAG is not an
automatic guarantee. These claims are **REFUTED as fidelity evidence** even
though the gap counts remain useful timing evidence.

The earlier adaptive suggestion (night 30 / day 50 / otherwise 40 minutes) was
not adopted: the same artifact concludes a single 55-minute timeout is simpler,
has a 7.5% false rate, leaves five minutes before the measured 60-minute cache
TTL, and adaptive timing saved only about $0.78/month. The new requirement
changes the objective from cache economics to zero daytime disruption, but does
not create evidence for shortening the night idle gate.

## Orchestra comparison

Orchestra already implements a cross-midnight window with IANA timezone
validation:

- default `21:00–06:00 Asia/Krasnoyarsk`;
- start inclusive, end exclusive;
- manual compact remains available outside the window;
- the timer rechecks idle status, active background jobs, known context, and
  threshold before compacting.

Relevant code:
`/mnt/data/Projects/Python/orchestra/app/session.py:108-169,302-359,418-531`
and tests in `tests/test_session.py:1639-1845`.

This is useful prior art, not a policy to copy verbatim:

1. `21:00–06:00` is an Orchestra operator choice, not a Kesha measurement.
2. When Orchestra's timer fires outside its window,
   `_fire_precompact_timer()` clears the timer and does not schedule the next
   window. Kesha must keep an over-threshold session pending until the next safe
   night boundary.
3. Kesha has Telegram debounce, transcription, injection, and deferred batches
   that Orchestra's state model does not share.

Orchestra's pre-save research contains one durable finding: summary generation
already runs while the full context is present, so file persistence can occur
through tools in the same summary turn rather than paying for a second
full-context turn. Server-side parsing of free-form logs was judged too brittle
as the primary memory mechanism.[9]

## Claude Code / SDK controls

As of 2026-07-30, official Claude Code documentation states:

- `DISABLE_AUTO_COMPACT=1` disables automatic compaction while preserving
  manual `/compact`;
- `DISABLE_COMPACT=1` disables both and is therefore the wrong control here.[2]

The deployed package recorded by task #13 is Python SDK `0.2.128` with bundled
Claude Code `2.1.220`. The same environment-variable names are present in a
direct local binary inspection of older bundled Claude Code `2.1.81`, and the
current official reference documents them. Compatibility with production
`2.1.220` is therefore **CONFIRMED** by package measurement plus primary current
documentation, though the flag has not been toggled on production in this
research phase.[2][10]

Raw local control measurement:

```text
sdk 0.1.50
options_env True
2.1.81 (Claude Code)
DISABLE_AUTO_COMPACT
DISABLE_COMPACT
```

`ClaudeAgentOptions` accepts an `env` map; Kesha currently creates options
without one. The later implementation should make the disable flag explicit at
the SDK process boundary, not rely on a shell profile that can drift. A smoke
must assert `get_context_usage()["isAutoCompactEnabled"] is False` and fail the
deployment check if the CLI reports otherwise.

Disabling native auto-compact removes the last-resort safety net. Official
documentation says a full context can then reject turns with
`Prompt is too long`, recoverable through manual `/compact`.[4] For Kesha the
correct product behavior is a single friendly message asking for `/compact`;
silently violating the daytime rule is not an acceptable fallback.

## Recommended night/offline policy

### Definition

“Offline” cannot be observed directly through the Telegram Bot API. The
smallest defensible proxy is the conjunction:

1. current time is in **`[23:00, 08:00)` in
   `ZoneInfo("Asia/Krasnoyarsk")`**;
2. there has been **at least 55 minutes of no recorded conversation
   activity** (definition below);
3. `ChatState` is atomically revalidated as exactly `IDLE`, with no pending or
   deferred entries, pending transcriptions, active processing/injection, stop,
   or compact;
4. context usage is known and at least 20%;
5. the task #13 usage-limit latch is inactive.

Confidence in “offline” is **LIKELY, not certain**: 55 minutes is behavioral
evidence, not an online-presence signal.

For this policy, durable conversation activity means:

- accepting a user entry;
- admitting an `urgent_llm` reminder into `ChatState`;
- completing a user/reminder LLM turn and returning to quiescence.

Both `accept_entry()` and `run_urgent_prompt()` therefore need the same small
activity-recording hook, and completion moves the deadline forward so a long
answer is not immediately followed by compact while the user may still be
reading it. A `plain` reminder bypasses Claude/`ChatState` and does not move the
deadline. A `lazy_llm` reminder moves it only when it is delivered with the next
real user entry. Runtime phase/pending checks remain mandatory regardless of
the timestamp.

The `23:00–08:00` window is recommended over Orchestra's `21:00–06:00` because
it is the exact night segment measured for this user (`n=93`) and excludes the
artifact's 18:00–23:00 segment, where 38 of 119 old potential compacts occurred.
Start is inclusive and end exclusive. A message at 07:30 moves eligibility to
08:25, outside the window, so the attempt waits until the following night.

### One automatic owner

The two Python triggers should converge on one per-chat night scheduler:

- a completed turn or incoming activity only updates the durable last-activity
  basis and ensures one scheduler task exists;
- a high daytime context records/logs risk but never calls compact;
- the scheduler sleeps to the later of the inactivity deadline and the next
  night opening, then rechecks all conditions under the state lock;
- outside-window, busy, or new-activity outcomes schedule the next relevant
  boundary instead of dropping the obligation or queueing a compact behind an
  active turn;
- low context disarms until future activity;
- a compact failure is one terminal attempt for that idle/window episode, not a
  tight retry; task #13's usage latch prevents quota loops;
- after success, the reduced context prevents another attempt until subsequent
  activity.

`_maybe_auto_compact()` must stop performing compaction and become at most a
context-risk/scheduler signal. `_on_preventive_elapsed()` must not remain a
second policy implementation.

### Restart behavior

The current in-memory timer is insufficient. `messages.timestamp` in
`storage/messages.db` is UTC and survives restart, but a normal message is
written only when `_run_batch()` starts. A handler can acknowledge an accepted
entry and the process can restart before that write, so `MAX(timestamp)` alone
is not a hard inactivity guarantee.

For a single-user bot, the least correct durable state is an explicit tiny
`chat_activity(chat_id PRIMARY KEY, last_activity_utc, quiescent)` table in the
existing SQLite database. Changing the semantic message log or adding a
scheduler service is more invasive.

The activity writes are lifecycle state, not best-effort telemetry:

1. before a user/urgent entry is queued, injected, or processed, one transaction
   records `last_activity_utc=now, quiescent=false`;
2. only after the admitted work has fully returned to `IDLE` does another
   transaction record `last_activity_utc=now, quiescent=true`;
3. an admission-write failure means the entry is not admitted silently and the
   failure is surfaced/retried through the existing entry boundary;
4. a crash or completion-write failure leaves `quiescent=false`, which survives
   restart and prohibits automatic compact until a later successful activity
   cycle reaches `IDLE`.

Thus no process-local latch or second persistence mechanism is required, and a
stale timestamp can produce delay/unavailability but never a false “offline”
decision.

1. At startup, enumerate chat IDs from `chat_activity` that also have a numeric
   persisted session file. Do not use `ALLOWED`: it contains Telegram user IDs,
   while `ChatState` keys are chat IDs and those differ in group chats.
2. Reconstruct the inactivity deadline only from a durable row with
   `quiescent=true`. Missing/non-quiescent rows fail closed: wait for a new
   successful activity cycle rather than infer that the user is offline.
3. If restart occurs in daytime, schedule the next 23:00 boundary but do not
   compact.
4. If restart occurs at night and durable inactivity already exceeds 55
   minutes, perform one guarded eligibility check.
5. At that night check, explicitly resume/connect the existing SDK session
   before asking for authoritative context usage. Current
   `get_context_usage()` returns only its in-memory cache while disconnected,
   and that cache is empty after restart.
6. Never infer inactivity from process uptime and never treat an unknown
   context percentage as permission to compact.

This covers an over-threshold context until the safe night without periodically
polling it. A production restart does not itself become “user went offline.”

### Manual compact

`/compact` retains current semantics:

- no time or inactivity gate;
- if processing, preserve manual provenance and execute after the turn;
- retain task #13 transactional SID replacement and usage-limit behavior.

Automatic provenance must never overwrite a queued manual request.

## Compact-summary quality design

### Evidence-based principles

Anthropic recommends maximizing recall first on complex traces, then removing
superfluous content; architectural decisions, unresolved bugs, and
implementation details should survive while redundant tool output is
discarded. Structured external notes provide continuity across resets.[11]

The official server-side compaction API independently exposes two useful design
ideas:

- custom instructions replace the default rather than supplementing it, so a
  custom prompt must state the complete preservation contract;
- `pause_after_compaction` can retain the latest messages verbatim after the
  summary, demonstrating that recent verbatim context is distinct from the
  compressed history.[12]

Kesha does not use that Messages API beta. These are prompt-design principles,
not a recommendation to replace the Agent SDK flow.

### Problems in the current prompt

`compact.py:12-36` is structured and specific, but:

- “be thorough” plus 5–10 detailed exchanges has no relevance filter and can
  become transcript dumping;
- it does not distinguish explicit fact from inference or unresolved conflict;
- it asks to put user preferences into `CLAUDE.md`, mixing personal memory with
  operational instructions;
- it does not forbid secrets or unsupported paths;
- it does not make file writes idempotent, so a rolled-back/retried compact may
  duplicate notes;
- `FILES` does not require exact state (read/changed/generated/tested);
- `PENDING` does not require blocker and next executable action;
- `RECENT` paraphrases instead of preserving the newest user wording.

### Proposed prompt contract

The recommended prompt below is deliberately complete because custom compact
instructions replace the defaults:

```text
[SYSTEM: Create a loss-minimizing handoff before context replacement]

You still have the full conversation. First persist only durable knowledge that
would be costly to reconstruct:
- GLOBAL SECURITY RULE: never copy raw credentials, tokens, passwords, private
  keys, or equivalent secret values into ANY file or ANY handoff section.
  Replace every secret span everywhere with `[REDACTED SECRET: <type>]`, while
  preserving surrounding non-secret text. This rule overrides every request
  for exact or verbatim content below.
- Update an existing canonical Markdown note under the current
  cog-second-brain working directory when the conversation established a
  durable fact, decision, project state, or TODO that is not already recorded.
- Keep CLAUDE.md for stable operating rules only. Never put personal facts,
  one-off requests, or secret values there.
- Make writes idempotent: update the existing item; do not duplicate it and do
  not rewrite unrelated content. If no correct destination is known, preserve
  the item in the handoff instead of inventing a path.

Then output ONLY the handoff below. Every statement must be supported by the
conversation or a tool result. Preserve disagreement and uncertainty; never
guess to fill a gap.

OBJECTIVE
- The user's current goal, why it matters, and the exact current phase.

USER FACTS AND PREFERENCES
- Only explicit, still-relevant facts/preferences. Mark one-off instructions as
  one-off. Do not include secrets.

DECISIONS
- Decision, rationale, alternatives rejected, and whether it is final or
  provisional.

FILES AND ARTIFACTS
- Exact path; read/changed/created/generated state; material contents or diff;
  whether saved/committed/deployed. Never invent a path.

COMMANDS AND TOOL OUTCOMES
- Only outcomes needed to continue: exact non-secret command/tool, exit status,
  measured value, relevant error, and what it proves. Redact secret arguments
  under the global rule. Drop redundant raw output.

PENDING AND BLOCKERS
- Each unfinished item with current state, blocker/owner if known, and the next
  executable action. Do not mark work complete without evidence.

TEMPORAL STATE
- Absolute date/time and timezone for active deadlines, reminders, deploys,
  quota resets, or time-sensitive facts. Say "as of" when freshness matters.

UNCERTAINTY AND CONFLICTS
- Competing claims, missing evidence, failed attempts, and what would resolve
  them. Do not collapse them into a false consensus.

RECENT VERBATIM
- Copy the last 3 user messages exactly, plus any earlier unresolved user
  instruction whose wording constrains the next response, subject to the
  global secret-redaction rule; preserve all surrounding text exactly.
- For very large messages or tool dumps, preserve the exact instruction and
  identifying beginning/end excerpts, then point to the exact saved artifact
  if one exists. Do not dump large raw outputs.

CONTINUATION
- The single next action the next session should take. If waiting for the user,
  say exactly what input is needed.

Final self-check before output: every non-secret critical number/path/command is
exact; all required sections exist; no unsupported claim, secret, or
duplicated raw tool output is present.
```

This prompt keeps the current one-turn pre-save design. Runtime should only
validate that the summary is non-empty and contains the required section
headers; semantic quality belongs in fixture evaluation, not a brittle parser.
Task #13's transaction remains the authority: no valid summary and continuation
result means no SID replacement.

### Preservation rubric

Score each fixture at the level of atomic anchors, not “sounds complete”:

| Category | Pass condition |
|---|---|
| Objective/current phase | Exact goal and phase preserved. |
| User facts/preferences | 100% of explicit critical anchors; one-off vs durable correctly labeled. |
| Decisions | Decision, rationale, rejected alternative, and provisional/final state preserved. |
| Files/artifacts | 100% exact paths and correct read/changed/committed/deployed state. |
| Commands/tools | Exact command/tool, exit/result, and measured value for every marked critical outcome. |
| Pending/blockers | Every unfinished critical item has state, blocker if known, and next action. |
| Temporal state | Absolute timestamp/timezone and “as of” qualifier preserved where supplied. |
| Recent wording | Last 3 ordinary-size user messages byte-for-byte identical after normalization of line endings, except secret spans replaced by the specified typed redaction marker. |
| Conflict/uncertainty | Both sides retained; no unsupported resolution. |
| Durable pre-save | Correct existing note updated once; retry produces no duplicate; unrelated file content unchanged. |
| Fabrication | **0** summary claims without a source anchor. |
| Secret handling | **0** seeded secret values in summary or newly written notes. |
| Bloat | No full redundant tool output; every retained block maps to at least one rubric anchor. |

Any miss in files/commands/pending/recent/conflict, any fabrication, or any
secret leak is a fixture failure. An aggregate prose-similarity score cannot
override these hard failures.

### Fixture set

The minimum deterministic corpus should contain:

1. **Decision and reversal:** an early choice, contrary evidence, a later
   provisional replacement, and the rejected alternative.
2. **Explicit preference vs one-off request:** one durable user preference and
   one request scoped only to the current turn.
3. **Exact paths and statuses:** similarly named files, one only read, one
   edited, one committed, one not deployed.
4. **Command evidence:** successful and failed commands with close numeric
   results and distinct exit codes.
5. **Pending/blocker:** completed substep plus unresolved external dependency
   and exact next command.
6. **Temporal state:** Krasnoyarsk local deadline, UTC tool timestamp, stale
   price/status marked “as of”.
7. **Recent-message continuation:** last three short user messages include a
   correction whose exact wording changes the next response.
8. **Long-output pressure:** large redundant logs around one critical error
   line and one measured value.
9. **Uncertainty/conflict:** two sources disagree and neither is decisive.
10. **Security and idempotence:** a recent user message contains a seeded fake
    token/private key plus surrounding non-secret instructions, and an existing
    durable note already contains one of the facts; compact is run twice. The
    expected verbatim value is the exact message with only the secret span
    replaced by its typed redaction marker.

For each fixture, store the transcript, required atomic anchors, forbidden
claims, forbidden secret strings, expected durable-file diff, and recent
verbatim strings. Run at least three generations per prompt revision because
the model is stochastic. Promote a prompt only when every run passes every hard
anchor; then optimize verbosity while keeping recall fixed. This follows
Anthropic's “recall first, precision second” recommendation.[11]

## Acceptance metrics for implementation

### Scheduling and ownership

1. At 20%, 95%, and 100% context, automatic compact calls outside
   `[23:00,08:00)` are exactly **0**.
2. `get_context_usage()["isAutoCompactEnabled"]` is `False`; a synthetic
   `compact_boundary(trigger="auto")` is treated as a failed invariant in
   verification.
3. Manual `/compact` at noon still invokes the existing transactional flow.
4. No auto compact occurs in `COLLECTING`, `WAITING_MEDIA`, `PROCESSING`,
   `STOPPING`, `COMPACTING`, or with non-empty pending/deferred/transcription
   work.
5. A user message racing the final eligibility check cancels/defers automatic
   compact to the next safe night, never queues it behind that turn.
6. Daytime restart with high context performs zero compact and preserves the
   SID; the next eligible night performs at most one attempt.
7. Night restart compacts only when the durable last conversation activity is
   at least 55 minutes old and a fresh connected context query meets the
   threshold; missing activity/context fails closed.
8. Outside-window timer expiry survives as a scheduled next-window check rather
   than disappearing.
9. Limit/failure has no tight retry, leaves the original SID durable and
   resumable, and task #13 progress terminalization remains intact. The failed
   summary request can remain as an extra turn in the original transcript, and
   any completed pre-save file writes remain as idempotent side effects; the
   contract does not falsely promise byte-identical context.
10. An overlapping manual and automatic request executes the manual request;
    automatic provenance cannot suppress it.
11. Admission writes `quiescent=false` before work; only successful return to
    `IDLE` writes `quiescent=true`. Failed admission admits no unrecorded entry;
    crash/completion-write failure stays non-quiescent across restart and can
    never authorize compact.

### Summary quality

1. All ten fixture classes pass every hard rubric item in three runs.
2. Fabricated atomic claims: **0**.
3. Seeded secret strings persisted or summarized: **0**.
4. Exact critical paths/commands/numbers/pending anchors: **100%**.
5. Last three ordinary-size user messages: **100% verbatim** after newline
   normalization, except exact secret spans replaced by typed redaction
   markers.
6. Repeated pre-save produces the same semantic file state with no duplicate
   entry.
7. Empty/malformed summary or failed continuation produces no durable SID
   replacement.

## Alternatives

| Alternative | Benefit | Blocking weakness | Verdict |
|---|---|---|---|
| Add hour check only to `_maybe_auto_compact` | Tiny diff | Preventive and native owners still compact during day | Reject |
| Gate both Python triggers, keep CLI auto | Preserves hard-limit safety | Native compaction still violates the guarantee | Reject |
| Inactivity only | Uses current timer | Still compacts during day | Reject |
| Window only | Simple | Can compact active night conversation | Reject |
| Periodic minute poll | Easy restart behavior | Repeated usage calls and loop/thrash surface | Reject |
| Fixed window + inactivity + single one-shot scheduler + native auto disabled | One policy, testable races, small state surface | Daytime hard-limit errors require manual recovery | **Recommend** |
| Migrate to Messages API server-side compaction | Native pause/recent-message features | Replaces current OAuth Agent SDK/persistent-session architecture | Reject for this MVP |

## Affected files for a later plan

Likely minimal surface:

- `claude_session.py` — explicitly disable native auto-compaction in SDK
  options and expose/verify reported state;
- `chat_state.py` — replace both automatic decisions with one night/offline
  scheduler and atomic eligibility recheck;
- `message_log.py` — add the explicit one-row-per-chat `chat_activity` table
  (`last_activity_utc`, `quiescent`) and lifecycle upsert/read methods;
- `bot.py` — arm configured chats on startup;
- `compact.py` — replace the handoff prompt and minimal required-section
  validation;
- `response_stream.py` — map the daytime hard-context failure to one friendly
  manual-`/compact` instruction without resetting the session;
- `system_prompt.txt` — describe night-only automatic/manual behavior, not the
  obsolete immediate 95% claim;
- `config.py`, `.env.example` — explicit IANA timezone/window if configurability
  is retained;
- `tests/` — boundaries, restart, injection/provenance races, native-disable
  invariant, and summary rubric fixtures.

No new generic scheduler framework, state-machine library, scheduler database,
or transcript parser is justified for one user. The single `chat_activity`
table is explicitly part of the minimal restart-safe design.

## Risks and counter-evidence

- **Hard-limit availability:** no daytime automatic safety net means turns can
  fail before night. This is an intentional requirement tradeoff; manual
  `/compact` and a friendly error are required.[4]
- **Offline is inferred:** inactivity cannot prove the user is asleep. The AND
  policy reduces but cannot eliminate this uncertainty.
- **Historical data is not freshly reproducible:** the raw database/extractor
  is absent. The 23:00–08:00 segment is strong retained evidence but not a live
  behavioral model.
- **Durable file side effects precede commit:** a pre-save can succeed and the
  summary later fail. Writes must therefore be useful and idempotent
  independently of SID replacement.
- **Personal-data leakage:** `cog-second-brain` is a personal knowledge base.
  The prompt must not turn every chat fact into a durable file and must never
  persist secrets.
- **RAG is recovery, not continuity:** retrieval can help after omission but
  cannot be counted as a passed summary anchor.
- **Model variability:** a good prompt is insufficient without multi-run
  fixtures. Runtime header validation detects malformed output, not semantic
  forgetting.
- **Orchestra precedent is incomplete:** its window gate drops an
  outside-window timer rather than carrying it to the next opening.

## Findings and confidence

1. **CONFIRMED — daytime compact currently has three possible owners.** Direct
   code inspection plus official SDK lifecycle docs.
2. **CONFIRMED — `DISABLE_AUTO_COMPACT=1` preserves manual `/compact`.**
   Current official primary documentation and binary/package measurements.
3. **CONFIRMED — current custom compact is session-transactional after task
   #13, but semantic omissions remain irreversible for active context.** Direct
   code inspection and official session behavior.
4. **LIKELY — `[23:00,08:00)` AND 55-minute inactivity is the best observable
   offline proxy.** User-specific retained measurements support the window;
   direct online presence is unavailable.
5. **CONFIRMED — restart needs durable activity recovery.** Current timers and
   registry are memory-only; message timestamps are already durable.
6. **CONFIRMED — one automatic scheduler is simpler and safer than keeping the
   20% and 95% paths independent.** Direct state/race analysis.
7. **LIKELY — the proposed prompt improves preservation.** It follows primary
   Anthropic guidance and closes observed prompt gaps, but must pass the defined
   multi-run fixture suite before implementation confidence becomes confirmed.
8. **REFUTED — “91% assistant-last means zero information loss.”** That
   historical label does not measure handoff recall.

## Sources

1. **Direct retained measurement / local primary artifact:**
   `artifacts/cache-compact-analysis.html:239-440`,
   `docs/tasks/cache-compact/calc.py`,
   `docs/tasks/cache-compact/timing_sweep.py`; commits `0c30fe2`,
   `3e056bf`, `a75ffc5`.
2. **Primary official:** Claude Code environment variables —
   https://code.claude.com/docs/en/env-vars
3. **Direct code measurement:** `chat_state.py`, `message_log.py`,
   `bot.py`, `compact.py`, `claude_session.py`, `handlers.py`.
4. **Primary official:** Claude Code error reference —
   https://code.claude.com/docs/en/errors
5. **Primary official:** Agent SDK loop and `compact_boundary` /
   `PreCompact` — https://code.claude.com/docs/en/agent-sdk/agent-loop
6. **Primary official:** context-window survival rules —
   https://code.claude.com/docs/en/context-window
7. **Primary official:** How Claude Code works —
   https://code.claude.com/docs/en/how-claude-code-works
8. **Primary official:** session resume/summary behavior and JSONL caveat —
   https://code.claude.com/docs/en/sessions
9. **Local primary implementation/research:**
   `/mnt/data/Projects/Python/orchestra/app/session.py`,
   `/mnt/data/Projects/Python/orchestra/docs/tasks/precompact-save/research.md`,
   `/mnt/data/Projects/Python/orchestra/docs/research-context-full.md`.
10. **Direct production measurement artifact:**
    `docs/tasks/13/report.md:148-168` (`claude-agent-sdk=0.2.128`,
    bundled Claude Code `2.1.220`).
11. **Primary official Anthropic engineering:** Effective context engineering
    for AI agents —
    https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
12. **Primary official Anthropic API reference:** server-side compaction —
    https://platform.claude.com/docs/en/build-with-claude/compaction
