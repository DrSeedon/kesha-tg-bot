# #25 — Report: one automatic retry on `runtime_unhealthy`

## What was wrong

When the reserve probe found an unresponsive runtime (#20: two 10 s probes → reconnect),
`_run_batch` sent «Рантайм не отвечает… повтори через полминуты» and returned. The user had to
retype. Both production events show the rejected batch was **a message the bot had already
accepted** — on 06.08 it was a drained `deferred` entry the user had sent 46 s earlier:

```
10:35:26 received msg_id=32017 preview='Напоминалку на слип лог убери я и так скидывать буду...'
10:35:26 deferred 1 entry during processing
10:35:52 phase processing → processing [drain_deferred n=1]
10:36:12 batch rejected before query (runtime_unhealthy)
```

Work accepted, then silently discarded — the orchestrator independently confirmed **0 rows** in
`messages.db` for that window, so the message left no trace at all.

## What changed

One extra `check_context_reserve(combined)` when — and only when — the first call returned
`runtime_unhealthy`. No sleep.

- **Retry succeeds** → normal path (`log_user` → `_ask_fn`). The user sees no notice at all.
- **Retry fails** → notice for the reason the *retry* returned. A second `runtime_unhealthy`
  refuses; a `reserve` result refuses as `context_reserve` **and sets the latch** — #14 fail-closed
  intact.
- **Only this reason.** `reserve`, `usage_limit`, `runtime_invariant`, `session_unavailable`,
  `unknown` keep single-shot behaviour.

### Why no delay is correct (measured, not assumed)

```
attempt1 (CLI SIGSTOPped): 20.0s ok=False reason=runtime_unhealthy client=None
retry1 (after SIGCONT):     8.3s ok=True  sid_preserved=True

retry while the old process is STILL frozen:
  15.4s ok=True  newpid=31554 oldpid=30415 same=False
```

`reconnect()` already dropped the bad client, so the retry builds a **fresh CLI process**. We are
not waiting for anything to recover — a backoff would only add latency.

### Why no duplicate is possible

The gate sits before every side effect (`chat_state.py`):

```
887 lazy_block, lazy_ids = self._get_lazy_block(...)   # pure read
897 reserve = await check_context_reserve(combined)
898 if not ok: _send_batch_terminal(...); return       <-- reject here
968 log_user(...)              # never reached
971 await self._ask_fn(...)    # never reached
974 mark_lazy_delivered(...)   # never reached
```

`get_lazy_block_for_prompt` only reads (`reminders.py:357`, its own docstring: *"Caller must call
mark_lazy_delivered() after successful _ask_fn"*), so reminders stay undelivered and are re-read,
never re-delivered. `phase = PROCESSING` is held for the whole `_run_batch`, so the retry cannot
race incoming messages — they defer exactly as today and drain once in `finally`.

## Text change

The old string promised «повтори через полминуты», which after this change is a lie — we already
retried. New wording states the fact and drops the false timing:

- **ru:** «⚠️ Рантайм не отвечает — не смог проверить свободный контекст даже со второй попытки,
  поэтому сообщение не отправил. Клиент переподключён, отправь ещё раз.»
- **en:** "⚠️ The runtime is not responding — I could not verify free context even on a second
  attempt, so I did not send your message. The client has been reconnected; please send it again."

Also dropped the hardcoded "Claude"/«Рантайм Claude» — a leftover in my own #20 string that #16
had already made runtime-neutral everywhere else. Not parameterised with `{runtime}`: that needs
`fmt` wiring at the call site, which is scope creep for a wording fix.

## Files

| File | ± | What |
|---|---|---|
| `chat_state.py` | +15 | the scoped one-shot retry in `_run_batch` |
| `config.py` | +2/-2 | reworded `context_runtime_unhealthy`, ru + en |
| `claude_session.py` | +5/-2 | comment only — the stale "Never rescue this batch" rationale |
| `tests/test_runtime_limits.py` | +115 | 9 new tests (5 parametrised) |
| `docs/tasks/25/` | new | research.md, report.md, spikes/ |

## Tests — 508 passed, 1 skipped

Run 3× consecutively, stable, and verified to leave **0 rows** in `storage/messages.db` for the
test chat each time (see "bug found in my own tests" below).

Mutation matrix — revert the guard, the test must go RED. All 5 confirmed:

| # | Mutation | Result |
|---|---|---|
| M1 | remove the retry | `test_runtime_unhealthy_retries_once_and_succeeds_silently` RED |
| M2 | retry on ANY failing reason (**fail-closed guard**) | `test_other_reasons_are_never_retried` RED ×5 |
| M3 | `while` instead of one shot | `test_runtime_unhealthy_twice_refuses_once_and_stops` RED |
| M4 | report the FIRST reason, not the retry's | `test_retry_reports_the_reason_the_retry_actually_returned` RED |
| M5 | retry through the `_context_reserve_blocked` short-circuit | `test_reserve_latch_short_circuit_does_not_trigger_a_retry` RED |

## Bugs found in my own work before commit

1. **M3 hung instead of failing.** The unbounded-retry mutant spun forever and pytest timed out
   (`exit=124`) rather than reporting a failure — a guard that hangs is a weak guard. Fixed by
   making the test stub raise once it is probed more times than the script allows, so an unbounded
   loop now fails fast with `reserve probed N times — retry is unbounded`.
2. **One exploratory run wrote to the real `storage/messages.db`** (a `chat_id=42` row), which then
   made my own #21 assertion `recent_rows_seen == [[]]` fail. Root-caused rather than papered over:
   the committed tests monkeypatch `message_log.get_db`, the stray row came from an earlier run
   *before* I added that patch. Row deleted; full suite now verified clean 3× in a row. Worth
   noting the row had `message_id: None`, so `_recent_user_rows` would have filtered it anyway —
   the assertion was over-specific, but the leak was the real problem.

## Breaking

None. New behaviour only on `runtime_unhealthy`; all other reasons unchanged.

## Codex

**No verdict — quota exhausted until 2026-08-08** (today 06.08). Not run, not substituted.

## Lesson (reusable)

A mutation test that makes the code *hang* has not proven anything — timing out is not the same as
failing, and in a CI log the two look nothing alike. When the mutant you plan is "unbounded loop",
give the test double a hard call ceiling that raises, so the guard reports the defect instead of
hanging on it.
