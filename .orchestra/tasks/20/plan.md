# #20 — Plan: bounded context probe, honest reason, self-healing client

Approved at the gate: items 1–3 + reconnect bundled. Fail-closed stays.

## Approach

`check_context_reserve` keeps refusing whenever it cannot measure. What changes is how long it
takes to find out, what we tell the user, and whether we leave the runtime broken for next time.

Probe budget **10 s** (≈3× worst measured healthy latency 3.45 s, vs SDK default 60 s), **one**
retry, then refuse with a new reason `runtime_unhealthy`. On the *second* consecutive timeout the
client is known-bad → `reconnect()` so the NEXT message is healthy. The current batch is still
refused — we never try to rescue it.

### Leak-safe probe (measured, non-obvious)

Naive `asyncio.wait_for(client.get_context_usage(), 10)` **leaks** one `pending_control_responses`
entry per timeout (measured 0→1→2→3, never reclaimed). Our outer cancel kills the SDK coroutine
before its own `fail_after(60)` cleanup at `query.py:589-591` can run.

Fix: don't cancel it — stop *waiting* for it. Launch the probe as a task, await it under
`asyncio.shield` with our 10 s budget. On our timeout we return immediately; the orphan task lives
on and the SDK's own 60 s handler removes the pending entry (measured: back to 0). A done-callback
retrieves the exception so asyncio doesn't log "never retrieved".

## Files

- **`claude_session.py`** — `_probe_context_usage()` helper (bounded, leak-safe); rework the probe
  block in `check_context_reserve` (`:643-653`); new `CONTEXT_PROBE_TIMEOUT_S = 10.0`.
- **`config.py`** — `context_runtime_unhealthy` in **both** `ru` and `en`.
- **`chat_state.py`** — reason→key mapping in the batch path (`~:636`) **and** the manual `/compact`
  path (`~:780`).
- **`response_stream.py`** — reason→key mapping in the retry preflight (`~:409`).
- **`tests/test_claude_session_limit.py`**, **`tests/test_activity_ingress.py`** — new tests.

**Not touched:** `get_context_usage()` (`:725`) and `_last_ctx_usage` — the cache stays unused by
the reserve path (research F5). Compact/latch/replacement logic unchanged.

## Reconnect safety (checked before planning, per gate instruction)

`reconnect()` sets `_client = None` and keeps `session_id` — `preserve_session` requirement met;
`_invalidate_session()` is NOT called, so the durable SID survives.

One real interaction found: `rollback_session_replacement` decides `source_unchanged` partly via
`self._client is snapshot.client` (`:224-227`). A reconnect during an active replacement would flip
that to False. In practice batches are deferred during `COMPACTING` so the paths don't overlap, but
that is an ordering accident, not a guarantee. **Guard explicitly**: skip the reconnect while
`self._session_replacement is not None`. Cheap, local, and removes the coupling instead of relying
on phase ordering. No other compact/latch state is touched → no STOP required.

## Tickets

### T1 — Bounded, leak-free probe with one retry
- Files: `claude_session.py`, `tests/test_claude_session_limit.py`
- AC:
  - a probe that exceeds `CONTEXT_PROBE_TIMEOUT_S` does not block the caller for the SDK's 60 s;
  - two consecutive timeouts → `{"ok": False, "reason": "runtime_unhealthy"}`;
  - a probe that succeeds on the *retry* admits the message normally (no false refusal);
  - N outer timeouts leak **zero** permanent `pending_control_responses` entries;
  - non-timeout exceptions keep current behaviour (`session_unavailable` / `unknown`).
- blocked-by: none

### T2 — Reconnect the known-bad client after the second timeout
- Files: `claude_session.py`, `tests/test_claude_session_limit.py`
- AC:
  - reconnect fires only after the second consecutive timeout, never after one;
  - `session_id` is preserved across it (durable SID untouched);
  - the batch is still refused (`ok == False`) — reconnect never rescues the current message;
  - no reconnect while `_session_replacement` is active;
  - an explicit log line records it.
- blocked-by: T1

### T3 — Honest user-facing reason
- Files: `config.py`, `chat_state.py`, `response_stream.py`, `tests/test_activity_ingress.py`
- AC:
  - `runtime_unhealthy` renders in the batch path, the `/compact` path and the retry preflight
    without `KeyError` (the #14 failure: one path updated, others not);
  - key exists in `ru` **and** `en`;
  - text says the runtime is not responding — not "resend shortly".
- blocked-by: T1

### T4 — Hard constraint #14 is intact
- Files: `tests/test_claude_session_limit.py`
- AC:
  - genuinely full context (`remaining < required`) → still refused with `reason="reserve"`;
  - `runtime_invariant` latch still refuses;
  - no new path admits a message without a fresh successful measurement.
- blocked-by: T1

## Mutation checks (required)

Each guard: revert the fix → the test must go RED; restore → GREEN.
1. Remove the retry → "succeeds on retry" test goes red.
2. Reconnect after the first timeout → "not after one" test goes red.
3. Use the leak-prone `wait_for` → leak test goes red.
4. Admit on timeout (fail-open) → hard-constraint test goes red.

## Codex

Quota exhausted until Aug 8 (`You've hit your usage limit`, `turn.failed`). No verdict obtainable;
recorded in `codex-review-research.md`. Self-review substitutes — it already caught the leak, which
was the single most dangerous item in this change.
