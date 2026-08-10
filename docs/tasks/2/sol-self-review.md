# #2 — strict Sol self-review

## Verdict provenance

This is an adversarial self-review by the implementing Sol worker, not an
independent Codex verdict. The mandatory external review was attempted once and
was unavailable with the exact platform result:

```text
weekly_quota_upgrade_required: New Codex worker turn blocked: the FastAPI readiness server does not provide worker-weekly-v1. Deploy the compatible FastAPI server before this MCP client; stop/model change remain available.
```

No restart, bypass, Claude review, push, deploy, or production mutation was used.

## Scope reviewed

Full diff from `origin/main=344cae3d17e06df6cb2d37550c0082de90cf391e`,
including the exact source patch
`b689e18ec65bbf0d0d2cbafb53ff527a5202f9be` (stable patch-id
`a6cbca70716ace815fbe7b8080552d915a5f0368`) and the verifier's terminal-cleanup
fix. Mail and nested MCP dispatch are outside #2 and were not reviewed or changed.

## Findings and disposition

### Fixed — lazy reconnect was not terminal cleanup

The source patch cancelled `safe_disconnect()` at 10 seconds and called
`ClaudeSession.reconnect()`. That method only moved `_client` to
`_pending_disconnect`; after successful Claude→Codex adoption the retired
session had no future owner to run `_ensure_connected()`, so the CLI process
could remain alive. The final code shields one terminal disconnect task from the
switch deadline, holds a strong per-chat reference, and retrieves its result.

### Fixed — pending-only Claude clients were skipped

`ClaudeSession.safe_disconnect()` originally selected only `_client`. A Claude
session already in `_client=None, _pending_disconnect=<client>` state would
therefore report cleanup success without touching the pending process. The final
implementation atomically detaches both unique owners before its first await and
disconnects each identity once.

### Fixed — shutdown could cancel terminal escalation

A detached cleanup task was initially owned only while the event loop remained
alive. `ChatRegistry.shutdown()` now cancels and awaits an active switch handler,
then awaits every owned terminal cleanup before disconnecting the current
runtime and clearing chats. Behavioral order proof requires candidate terminal
cleanup before current-runtime teardown.

### Fixed — repeated delta-less completion duplicated text

The source fallback suppressed `item/completed` only when the item had emitted a
delta. A replayed delta-less completion for the same `(turn_id, item_id)` was
still emitted twice. One `text_items` set now records both delta and fallback
emission; a replay test requires exactly one visible chunk.

## Adversarial checklist

- Strong ownership: `_runtime_cleanup_tasks` retains every slow teardown until
  its done callback removes it.
- Exception retrieval: the callback calls `Task.result()` on success, failure,
  and cancellation paths; a delayed failure produces no loop-level unhandled
  task context.
- Shield/cancellation: the switch deadline waits on `shield(task)` and never
  cancels terminal escalation. Repeated cancellation of the outer switch is
  deferred until phase cleanup completes.
- Timeout completion: the switch releases after its fixed deadline while the
  terminal task continues and is later observed complete.
- No double disconnect: Claude owners are detached synchronously and deduped by
  identity; the blocking-client test observes exactly one disconnect call.
- Failed adoption: candidate-only cleanup leaves the incumbent session selected,
  connected, and able to drain deferred work.
- Shutdown: the tracked switch task is cancelled/awaited before cleanup snapshot;
  pending teardown completes before current session shutdown.
- No lost/double reply: abort and commit each call one deferred drain; terminal
  callbacks never drain. Delta, completion fallback, repeated completion, and
  stale-turn cases have separate behavioral coverage.

## Remaining limitations

- `thread/inject_items` is experimental in Codex app-server 0.146.0. Registry
  validation and bounded rollback contain schema drift but cannot make the API
  stable.
- Local verification has `claude-agent-sdk 0.2.114` while `requirements.txt`
  declares `>=0.2.128`; no dependency was installed. The changed lifecycle tests
  use explicit blocking clients and do not depend on SDK constructor fixtures.
- `tests/test_activity_ingress.py` cannot collect because
  `aiogram_media_group` is absent. This is recorded, not silently omitted.

Self-review verdict after fixes: **APPROVE within #2 scope**. This is not an
independent external verdict.

Final evidence: focused runtime suite `126 passed, 1 skipped`; full available
suite `511 passed, 1 skipped`; all nine independent mutations exited 1 and were
restored byte-for-byte; all 13 final mutation-guard tests passed. The separately
attempted activity-ingress file still fails collection only on missing
`aiogram_media_group`.
