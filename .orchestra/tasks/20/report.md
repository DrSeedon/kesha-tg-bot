# #20 — Report: bounded context probe, honest reason, self-healing client

## What was wrong

A user message was refused with «⚠️ Не удалось проверить свободный контекст» — but only *after the
bot appeared to think for a full minute*. `check_context_reserve` probes the runtime via
`get_context_usage()`; the SDK gives that control request a **60 s** default budget
(`query.py:547`, not overridden at `:774`). On timeout the reserve failed closed with the generic
`reason="unknown"`, telling the user to "resend shortly" — and the resend hit the same wedged
process for another 60 s.

**Not a #14 regression.** #14 behaved correctly here: `reason=unknown`, no latch, bot stayed alive.

## Root cause

A healthy control request answers in **0.9–3.4 s** (measured). The 60 s budget therefore never
expires because the runtime was "slow" — it expires when the runtime has stopped servicing the
control channel. Reproduced 1:1 by SIGSTOP-ing the CLI: `60.1s Control request timeout`, then
`3.3s ok` after SIGCONT.

Production log, the decisive detail — the pairs are exactly 60.0 s apart:
```
10:05:26 Reminder #24/#100 urgent_llm delivered ok
10:06:26 Control request timeout: get_context_usage   ← +60.0s
10:07:26 Control request timeout: get_context_usage   ← +60.0s
```

## Two hypotheses measured and REJECTED

Recording these because both were plausible enough to have shipped a useless fix:

- **"Timeout = transient slowness, just retry/wait longer."** Refuted by the 20–60× gap between
  healthy latency and the budget.
- **"A concurrent turn/reminder starves the control request."** Refuted directly: probes during
  active generation and with a second `receive_messages()` consumer all returned in 1–3 s
  (`spikes/ctxbusy.py`, `spikes/ctxrace.py`). Control requests are multiplexed over the same stdout
  stream. **A lock-based fix would have passed review and fixed nothing.**

## What changed

Fail-closed is unchanged: no measurement → no send.

1. **Own budget, 10 s** (`CONTEXT_PROBE_TIMEOUT_S`), ≈3× worst healthy latency. 60 s stall → 10 s.
2. **One retry.** Justified by measurement, not optimism: the SDK abandons the pending request on
   timeout, so attempt 2 is a clean request (3.3 s once the CLI recovered).
3. **New reason `runtime_unhealthy`** + a message that says the runtime is not responding, instead
   of "resend shortly" (which sent the user straight back into the same wall).
4. **Reconnect after the *second* consecutive timeout** — heals the client for the next message.
   The current batch is still refused; we never try to rescue it. `session_id` preserved.

### The non-obvious part: the leak I introduced and caught

Naive `asyncio.wait_for(client.get_context_usage(), 10)` **leaks one
`pending_control_responses` entry per timeout** — measured 0→1→2→3, never reclaimed. Our outer
cancel kills the SDK coroutine before its own `fail_after` cleanup (`query.py:588-590`) can run. On
a session that lives for weeks that is a slow poison.

Fix: don't cancel the call, just stop waiting for it — `asyncio.shield` + our own budget; the
orphan lives on and the SDK's own handler reclaims the entry (measured back to **0**).

## Files

| File | ± | What |
|---|---|---|
| `claude_session.py` | +45/-1 | `CONTEXT_PROBE_TIMEOUT_S`, `_ProbeTimeout`, `_probe_context_usage()`, reworked probe block |
| `config.py` | +2 | `context_runtime_unhealthy` in **ru and en** |
| `chat_state.py` | +4 | reason→key in the batch path **and** the `/compact` path |
| `response_stream.py` | +2 | reason→key in the retry preflight |
| `tests/test_claude_session_limit.py` | +136 | 7 new tests |
| `tests/test_activity_ingress.py` | +31 | render tests + exhaustive key/language check |

## Tests — 189 passed (was 181)

Mutation matrix (revert the guard → test must go RED). All confirmed:

| # | Mutation | Result |
|---|---|---|
| M1 | remove the retry | `test_probe_succeeding_on_retry_admits_the_message` RED |
| M2 | reconnect after the 1st timeout | `test_single_timeout_does_not_reconnect` RED |
| M3 | naive `wait_for` (cancels SDK call) | `test_probe_timeout_does_not_leak_pending_control_entries` RED |
| M4 | fail-**open** on timeout | 3 tests RED incl. the hard constraint |
| M5 | drop the `_session_replacement` guard | `test_no_reconnect_while_session_replacement_is_active` RED |

**End-to-end against a real wedged CLI** (`spikes/ctxe2e.py`, `spikes/ctxsid.py`) — not just fakes:
```
healthy reserve: ok=True remaining=975872 required=208002
wedged reserve:  20.0s -> ok=False reason=runtime_unhealthy   (was 60s)
client dropped=True   sid preserved=True   sid=80f3fa44-0d7a-4249-8e01-535092b527c2
file still: 80f3fa44-0d7a-4249-8e01-535092b527c2
after reconnect: 7.8s -> ok=True
```

## Reconnect safety (gate required this)

`reconnect()` clears `_client`, keeps `session_id`, does **not** call `_invalidate_session()` —
durable SID survives (verified above with a real UUID, in memory and on disk).

One coupling found: `rollback_session_replacement` decides `source_unchanged` partly via
`self._client is snapshot.client` (`:224-227`). A reconnect mid-replacement would flip it. Batches
are deferred during `COMPACTING` so the paths don't overlap today — but that is an ordering
accident, not a guarantee, so the reconnect is explicitly skipped while a replacement is active.
No compact/latch state touched → no STOP was needed.

## Breaking

None. New reason + new string; all existing reasons and behaviour unchanged.

## Codex

**No verdict — quota exhausted until 2026-08-08.** Recorded honestly in
`codex-review-research.md` rather than substituting a stand-in reviewer. The single most dangerous
item (the leak) was caught by measurement instead.

**Weakest remaining point, stated plainly:** SIGSTOP reproduces the *signature*, not proof of
prod's cause. A dropped `control_response` frame on a healthy CLI is indistinguishable from our
side. The fix degrades correctly either way (retry = new `request_id`; reconnect rebuilds the
transport), so it does not depend on that interpretation — but "the CLI was wedged" is inference.
The defensible claim is: the control channel stopped answering for ≥60 s, cause not established.

## Lesson (reusable)

A fix that removes a visible failure can add an invisible one. Bounding a third-party async call
from the outside cancels its internal cleanup — the timeout worked, and the cleanup silently
didn't. Measure the resource, not just the latency, before and after.
