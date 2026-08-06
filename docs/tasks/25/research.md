# #25 — Should a `runtime_unhealthy` batch be retried automatically?

## Question

- **Context**: `chat_state.py:912` (batch path) and `:1106` (manual `/compact` path). When
  `check_context_reserve` returns `runtime_unhealthy` (#20: two 10 s probes unanswered → reconnect),
  `_run_batch` sends a terminal notice and `return`s. Retry exists only *inside* the control request
  (`_probe_context_usage`), never at the message level.
- **Change under test**: retry the batch automatically after the reconnect, instead of asking the
  user to resend.
- **Baseline**: current behaviour — one notice, user retypes.
- **Outcome that decides it**: (a) does a retry actually succeed, (b) can it duplicate anything the
  user already received, (c) is the #14 fail-closed guarantee preserved.

## Hypotheses considered

| # | Hypothesis | Falsifier | Verdict |
|---|---|---|---|
| H1 | A retry after reconnect succeeds, so the refusal is avoidable | Wedge the CLI, reconnect, retry, measure | **CONFIRMED** |
| H2 | Retry needs a delay for the runtime to recover | Retry immediately with no sleep | **REFUTED — no delay needed** |
| H3 | Retry can duplicate a Claude turn (batch partially delivered) | Locate the reject relative to `log_user`/`_ask_fn` | **REFUTED** |
| H4 | Retry can double-deliver lazy reminders | Check whether `get_lazy_block_for_prompt` consumes state | **REFUTED** |
| H5 | Retry races incoming messages / the deferred queue | Check the phase held during `_run_batch` | **REFUTED** |
| H6 | The refusal is cosmetic — the user just resends and nothing is lost | Read what was actually rejected in both prod events | **REFUTED — real loss** |

## Findings

### F1 — The reject happens BEFORE anything is sent or logged. No duplicate is possible. CONFIRMED
Evidence tier 2 (source, `chat_state.py`, read this session). Order inside `_run_batch`:

```
882  combined = time_prefix + ...
887  lazy_block, lazy_ids, lazy_reschedule = self._get_lazy_block(...)   # pure read (F3)
895  reserve = await self.session.check_context_reserve(combined)
899  if not reserve.get("ok"):  ... await self._send_batch_terminal(...) ; return   <-- HERE
953  _get_msg_db().log_user(...)          # never reached on reject
956  await self._ask_fn(reply_msg, combined, self.chat_id)   # never reached on reject
959  mark_lazy_delivered(lazy_ids, ...)   # never reached on reject
```

The gate is a **pre-flight check**: on `runtime_unhealthy` no query was issued, no message was
logged, no reminder was marked delivered, and no assistant text reached Telegram except the notice.
So "the batch may have partially arrived" — the orchestrator's main worry — **cannot happen on this
reason**. This is the load-bearing finding for the whole proposal.

### F2 — An immediate retry succeeds; no sleep needed. CONFIRMED (H1 confirmed, H2 refuted)
Evidence tier 1 (direct measurement, live CLI, this session — `docs/tasks/25/spikes/`):

```
attempt1 (CLI SIGSTOPped): 20.0s ok=False reason=runtime_unhealthy client=None
retry1 (after SIGCONT):     8.3s ok=True  reason=None  sid_preserved=True
```

And the stronger case — retry **while the old process is still frozen**:

```
retry-while-old-still-STOPPED: 15.4s ok=True reason=None
  newpid=31554 oldpid=30415 same=False
```

`reconnect()` already dropped the bad client, so the retry builds a **new CLI process** and does not
touch the wedged one. That is why no backoff is required: we are not waiting for anything to
recover. One retry is sufficient and bounded.

### F3 — Lazy reminders cannot double-fire. CONFIRMED (H4 refuted)
Evidence tier 2 (source, `reminders.py:357-371`). `get_lazy_block_for_prompt` only *reads*
`fetch_lazy_undelivered` and returns ids; its own docstring says **"Caller must call
mark_lazy_delivered() after successful _ask_fn."** On the reject path `mark_lazy_delivered` is never
reached (F1), so the reminders stay undelivered and are legitimately re-fetched by the retry. A
retry re-reads them; it does not re-deliver them.

### F4 — A retry cannot race the deferred queue. CONFIRMED (H5 refuted)
Evidence tier 2 (source). `phase = PROCESSING` is set at `:690` before `_start_processing` and held
for the whole `_run_batch`; `accept_entry` (`:161-172`) appends to `self.deferred` for any entry
arriving during PROCESSING/STOPPING/COMPACTING. `_drain_or_idle` runs only in `finally`
(`_finish_processing`). A retry inside `_run_batch` therefore stays inside the same PROCESSING
window — new messages queue as they already do, and the drain still happens exactly once.

### F5 — The current behaviour loses a real message, it is not merely annoying. CONFIRMED (H6 refuted)
Evidence tier 1 (prod journal, both `runtime_unhealthy` events ever recorded).

Event 2026-08-06 (this is the one the user complained about):

```
10:35:26 received msg_id=32017 ... preview='Напоминалку на слип лог убери я и так скидывать буду...'
10:35:26 deferred 1 entry during processing
10:35:52 phase processing → processing [drain_deferred n=1]
10:36:12 batch rejected before query (runtime_unhealthy)
10:36:12 phase processing → idle [idle]
```

The rejected batch was the **drained deferred message** — a real question the user had already
sent 46 s earlier. Event 2026-08-04 is the same shape (`debounce_fire batch=1` → rejected 20 s
later). So the user is asked to retype something the bot already holds; on 06.08 it was a message
that had been waiting in the queue. That is why the complaint («почему выползает, некоторая такого
надо ретрай») is about lost work, not wording.

### F6 — Frequency is low and each event costs exactly 20 s. CONFIRMED
Evidence tier 1 (prod journal). `probe attempt 1 timed out` appears **2** times total, and both
escalated to two probes — i.e. **there is no population of single-probe timeouts that silently
recovered**; when the channel goes quiet it stays quiet for both probes.

```
Aug 04 18:55:26 attempt 1 timed out after 10s
Aug 04 18:55:36 attempt 2 timed out after 10s → reconnect → batch rejected
Aug 06 05:36:02 attempt 1 timed out after 10s
Aug 06 05:36:12 attempt 2 timed out after 10s → reconnect → batch rejected
```

2 events in ~2 days. Low enough that a *bounded* retry adds at most ~8–15 s twice per two days, and
high enough that the user already noticed.

### F7 — Retrying must be scoped to THIS reason only. CONFIRMED
Evidence tier 2 (source, `chat_state.py:901-917`). The same `if/elif` chain handles `reserve`,
`session_unavailable`, `usage_limit`, `runtime_invariant`, `runtime_unhealthy`, `unknown`. A retry
placed on the generic reject path would also retry:

- `reserve` — context genuinely full → retry burns another probe and refuses again; **breaks the
  spirit of #14's fail-closed** by hammering a known-full context.
- `usage_limit` — quota exhausted; retry is forbidden by the existing project rule
  ("Rate-limit/quota ошибки = ждать, НИКОГДА не retry").
- `runtime_invariant` — a proven contradiction (wrong model/window); retry cannot change it.

Therefore the retry must be gated on `reason == "runtime_unhealthy"` **and** on the reconnect
having actually happened, not bolted onto the shared branch.

## Counter-evidence / what argues against retrying

- **My own #20 comment says the opposite:** `claude_session.py:700` — *"Refuse (we still have no
  measurement) but drop the bad client so the NEXT message meets a healthy one. Never rescue this
  batch."* That was a deliberate decision, and I should not reverse it silently. What changed is
  evidence, not taste: F1 proves nothing was delivered, so "rescuing" is not a half-sent turn, and
  F5 proves the cost is a lost queued message. The #20 comment must be updated, or the code will
  contradict its own rationale.
- **Rarity argues for doing nothing** (F6, 2 events). A retry is ~15 lines and one more failure
  mode; "soften the wording" is ~1 line. Honest alternative, see recommendation.
- **The user asked for a retry, which is not automatically the right design.** But here it coincides
  with the measurement: the retry is cheap, cannot duplicate, and recovers a real message.
- **Unknown:** whether the underlying cause is a stopped process or a lost control frame remains
  unestablished from #20 (recorded there). The retry is correct for both — a new `request_id` on a
  new process — so it does not depend on resolving that.

## Recommendation (for the gate — NOT implemented)

**Retry once, scoped to `runtime_unhealthy`, inside `_run_batch`, no sleep.**

1. On `reason == "runtime_unhealthy"`, re-run `check_context_reserve(combined)` exactly once.
   No delay: F2 shows the retry spawns a fresh process and succeeds in 8–15 s.
2. Retry succeeds → continue the normal path (`log_user` → `_ask_fn`). The user gets an answer and
   never sees a notice.
3. Retry fails, or returns any other reason → send the terminal notice for the reason **actually
   returned** and stop. In particular a second `runtime_unhealthy` still refuses, and a `reserve`
   result on the retry refuses with `context_reserve` — #14 fail-closed intact.
4. **Only this reason.** `reserve`, `usage_limit`, `runtime_invariant`, `session_unavailable`,
   `unknown` keep today's single-shot behaviour (F7).
5. Soften the notice text as well — it currently promises "повтори через полминуты", which after
   this change is wrong: we already retried. Reword to say the runtime was unreachable and the
   message was not sent.
6. Update the stale `claude_session.py:700` comment so the code stops contradicting itself.

Worst-case added latency: one extra probe pair on a still-dead runtime = +20 s before the same
refusal, twice per two days. Acceptable against silently dropping a queued user message.

**Manual `/compact` path (`:1106`) — do NOT retry.** `/compact` is an explicit operator action with
no queued user text to lose; the user can re-issue it, and retrying a compaction gate touches the
#14 transaction. Out of scope.

## Affected files, risks, edge cases

- `chat_state.py` — `_run_batch` reject branch (~`:895-921`); the retry belongs here, not in
  `check_context_reserve` (which must stay a pure measurement).
- `config.py` — reword `context_runtime_unhealthy` in **both** `ru` and `en`.
- `claude_session.py` — comment only (`:700`), no behaviour change.
- `tests/test_activity_ingress.py` / `tests/test_claude_session_limit.py` — new tests.
- **Risk: double notice.** If the retry path is written carelessly the user could get two notices
  (one per attempt). Test must assert exactly one message per rejected batch.
- **Risk: retry loop.** Must be exactly one extra attempt, not a `while`. Test with a permanently
  wedged runtime asserting exactly 2 `check_context_reserve` calls.
- **Risk: scope leak.** A refactor could apply the retry to `reserve`/`usage_limit`. Mutation test
  required: force `reason="reserve"` and assert only one call.
- Edge: `_context_reserve_blocked` short-circuits to `{"ok": False, "reason": "reserve"}` at `:894`
  without calling the session at all — the retry must not fire there (it is `reserve`, not
  `runtime_unhealthy`).

## Sources

1. Direct measurement, this session — `/tmp/retry_probe.py`, `/tmp/retry_stuck.py` (copied to
   `docs/tasks/25/spikes/`), live Claude CLI, SDK 0.2.128. Output quoted verbatim above.
2. Production journal, `root@158.220.127.161`, `journalctl -u kesha-bot-vps` — both
   `runtime_unhealthy` events and their surrounding phase transitions.
3. Primary source, read this session: `chat_state.py` (`_run_batch`, `accept_entry`,
   `_drain_or_idle`, `_send_batch_terminal`), `reminders.py:357-378`, `claude_session.py:651-712`,
   `response_stream.py:446`.
