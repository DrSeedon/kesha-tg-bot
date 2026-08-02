# #20 — Is fail-closed correct for a control-request TIMEOUT?

## Question

- **Context**: `check_context_reserve` (`claude_session.py:600`) gates every outgoing batch. It
  calls `self._client.get_context_usage()` (`:644`) and, on any exception, returns
  `reason="unknown"` → the batch is refused and the user sees
  «⚠️ Не удалось проверить свободный контекст».
- **Change under test**: should a *timeout* of the control request keep failing closed, or should
  it retry / use cache / fail open?
- **Baseline**: current behaviour — one attempt, any exception → refuse.
- **Measurable outcome**: (a) how long a healthy control request takes, (b) what a 60 s timeout
  actually proves about the runtime, (c) whether a refused message could have been sent safely.

## Hypotheses considered

| # | Hypothesis | Falsifier | Verdict |
|---|---|---|---|
| H1 | Timeout = transient slowness; the runtime is fine, we just didn't wait long enough | Measure healthy latency. If it is ~1 s vs a 60 s budget, "too slow" is not credible | **REFUTED** |
| H2 | Timeout = contention: a concurrent turn on the same client starves the control request | Run a control request while `receive_messages()` drains an active turn | **REFUTED** |
| H3 | Timeout = the CLI process is not servicing stdin at all (hung/stopped/lost frame); it recovers on its own | SIGSTOP the CLI, measure; then SIGCONT and re-probe | **CONFIRMED** |
| H4 | `_last_ctx_usage` is a safe substitute when the probe fails | Check who writes it and when | **REFUTED — unsafe** |

## Findings

### F1 — A healthy `get_context_usage` answers in ~1–3 s. The timeout is 60 s. CONFIRMED
Evidence tier 1 (direct measurement, this session, SDK 0.2.128, live CLI):

```
probe0: 3.191s  probe1: 1.287s  probe2: 1.727s
probe3: 1.016s  probe4: 0.928s  probe5: 1.676s     (idle session)
```

The budget is 20–60× the observed latency. `_send_control_request` defaults to
`timeout: float = 60.0` (`_internal/query.py:547`) and `get_context_usage` passes **no override**
(`:774`), so the effective budget is 60 s.

**Consequence:** a timeout is never "we didn't wait long enough". H1 is refuted. A plain retry
"because it might be slow" is therefore not justified by latency.

### F2 — Contention with an in-flight turn does NOT cause the timeout. REFUTED
Evidence tier 1. Probed while a turn was streaming and while `receive_messages()` was being drained
by a second consumer (mimicking `send_message`'s `async for`, `claude_session.py:358`):

```
busy-probe0: 3.453s ... busy-probe7: 1.205s   (all ok, during active generation)
[drain] result / [probe0] 2.75s ok / [probe1] 1.09s ok / [probe2] 1.46s ok
```

Control requests are multiplexed over the same stdout stream and answered while the model is
generating. This matters: it kills the intuitive "the reminder turn was blocking it" story, which
would have led to a lock-based fix that fixes nothing.

### F3 — A timeout means the CLI is not servicing the control channel; it recovers by itself. CONFIRMED
Evidence tier 1. SIGSTOP the CLI child, probe, SIGCONT, probe again:

```
before kill: pct=2
STOPPED-cli: 60.1s ERR Control request timeout: get_context_usage
after CONT:   3.3s ok pct=2
```

**60.1 s reproduces the production signature exactly.** On timeout `_send_control_request` discards
the pending entry (`:589-591`), so the request is abandoned, not queued — a later call is a clean
new request, which is why the post-CONT probe succeeded in 3.3 s.

### F4 — Production log confirms F3, and the pairs are exactly 60 s apart. CONFIRMED
Evidence tier 1 (prod journal, `root@158.220.127.161`):

```
10:05:26  Reminder #24  urgent_llm delivered ok
10:05:26  Reminder #100 urgent_llm delivered ok
10:06:26  Context reserve control request failed: Control request timeout: get_context_usage
10:06:26  Chat 720740564: batch rejected before query (unknown)
10:07:26  Context reserve control request failed: Control request timeout: get_context_usage
10:07:26  Chat 720740564: batch rejected before query (unknown)
```

Two independent facts fall out:

1. **10:05:26 → 10:06:26 is exactly 60 s.** The probe was issued the moment the reminders finished
   and burned the full budget. Same for the 10:38:01/10:39:01 pair.
2. **The user waited through 60 s of silence before each refusal.** The typing indicator runs the
   whole time, so the perceived failure is "the bot hung for a minute, then refused" — worse than
   the message text suggests.

Frequency: 7 timeouts in 24 h; only the two on Aug 02 (post-#14) reached a user as a refusal.
The Aug 01 `runtime_invariant` pair is the old #14 bug, already fixed — unrelated.

### F5 — `_last_ctx_usage` is NOT a safe substitute. REFUTED (this is a trap)
Evidence tier 2 (source). The cache is written **only** in `get_context_usage()` (`:741`), which is
called from the idle auto-compact timer (`chat_state.py:543`), `handlers.py:160`, `kesha_tools.py:88`
and `compact.py`. **`check_context_reserve` never writes it** — it calls the client directly (`:644`).

So on the reserve path the cache is whatever the compact timer last stored, which fires after
**55 minutes of idle**. Every turn since then has grown the context without updating it. Using it as
the admission number means admitting on a measurement that is arbitrarily old and monotonically
too small — exactly the "«не знаю» подаётся как «проверено»" failure #14 was about, and it would
defeat the hard constraint. **Recommend against the cache option.**

### F6 — Fail-closed on timeout is directionally right but the *cost is misattributed*. LIKELY
The reserve exists to prevent an overflowing turn. On timeout we have no measurement, so we cannot
prove headroom → refusing is the safe default and must stay. But F1+F3 show a timeout is not a
"maybe" — it is evidence the CLI is **unhealthy**, and the current code treats that identically to
a parse failure or an unknown exception, telling the user "retry shortly" while doing nothing to
restore health. The user's retry then re-runs the same 60 s probe against the same wedged process
(F4, two consecutive refusals) — the bot burns 60 s per attempt to reach a predictable refusal.

## Counter-evidence / what argues against changing anything

- **Rarity.** 7 events / 24 h, 2 user-visible. A wrong fix here is more expensive than the bug.
- **Fail-open is unacceptable** even though "timeout ≠ full". Overflow is the failure #14 exists to
  prevent, and a wedged CLI is precisely when we understand the state least. I do **not** recommend
  fail-open, including "fail-open with a loud log" from the task's option list.
- **Against a naive retry**: F1 refutes the slowness premise. A retry is only justified because the
  abandoned-request path (F3) makes a *fresh* request cheap and independent — not because waiting
  longer helps. A retry must therefore be bounded and short, not another 60 s.

## Recommendation (for the gate — not implemented)

Keep fail-closed. Fix the two real defects instead: the 60 s stall and the missing distinction
between "unhealthy runtime" and "unknown".

1. **Bound the probe ourselves** — wrap the call in `asyncio.wait_for(..., ~10 s)`. 10 s is ~3× the
   worst healthy latency measured (3.45 s) with margin, vs the 60 s default. Turns a 60 s stall into
   a fast, honest answer. This is the main win and is independent of everything below.
2. **One short retry after the bounded probe.** Justified by F3: the request is abandoned, so a
   second one is clean and succeeded in 3.3 s once the CLI recovered. Total worst case ~20 s instead
   of 60 s (or 120 s across the user's own retry).
3. **Distinct reason `runtime_unhealthy`** for "probe timed out twice", separate from `unknown`, with
   a message that says the runtime is not responding rather than "resend shortly". Still refuses.
4. **Do NOT use `_last_ctx_usage`** (F5) and **do NOT fail open** (counter-evidence).

Optional, flag for the gate rather than assume: on double timeout the session is a known-bad client;
`reconnect()` would likely restore it, but that mutates session state on a path that currently only
reads, so I would not bundle it without approval.

## Affected files, risks, edge cases

- `claude_session.py` — `check_context_reserve` probe (`:643-653`); new bounded-timeout constant.
- `config.py` — new string in **both** `ru` and `en` (a missing key raises `KeyError` at send time).
- `chat_state.py` (`~:636`) and `response_stream.py` (`~:409`) — reason→key mapping in **both**
  places, plus the manual `/compact` path (`~:780`). #14 shipped a `KeyError` by updating only one.
- Risk: `asyncio.wait_for` cancels the awaited task; the SDK already cleans the pending entry on its
  own timeout, but our outer cancel happens *before* that — must confirm no leaked `pending_control_responses`
  entry accumulates per timeout (leak check belongs in the plan).
- Edge: `manual=True` (`/compact`) uses the same probe; a wedged CLI must not make `/compact`
  unreachable, since `/compact` is what we tell the user to run when the reserve is exhausted.

## Sources

1. Direct measurement, this session — `/tmp/ctxprobe.py`, `/tmp/ctxbusy.py`, `/tmp/ctxrace.py`,
   `/tmp/ctxdead.py`, SDK 0.2.128, live Claude CLI. Raw output quoted verbatim above.
2. `claude_agent_sdk/_internal/query.py:546-591` (`_send_control_request`, default timeout, cleanup),
   `:774` (`get_context_usage` sends no override) — installed 0.2.128, read this session.
3. Production journal, `root@158.220.127.161`, `journalctl -u kesha-bot-vps`, Aug 02 window.
4. Repo source read this session: `claude_session.py`, `chat_state.py`, `response_stream.py`, `config.py`.
