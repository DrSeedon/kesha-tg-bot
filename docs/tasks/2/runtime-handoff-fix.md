# #2 — bounded passive runtime handoff (2026-08-10)

## Production failures

1. Claude hit its monthly spend limit. The replacement Codex app-server started,
   passed its quota probe and created a thread, but `ChatState._deliver_handoff()`
   sent up to 24K characters through `send_message()`. That opened a normal agent
   turn. Codex continued an old email task, invoked tools and waited indefinitely;
   `ChatPhase.PROCESSING` remained latched and a later user message stayed deferred.
2. After the app-server was terminated manually, the deferred turn completed in
   Codex and its rollout contained the final `agent_message`, but Kesha logged
   `response 0 chars`. The adapter discarded every `item/completed` agent message
   on the assumption that `item/agentMessage/delta` had already arrived.

## App-server fact check

Local binary: `codex-cli 0.146.0`.

`codex app-server generate-json-schema --experimental` documents
`thread/inject_items` as appending raw Responses API items to the thread's
model-visible history. A live, isolated app-server probe used this payload:

```json
{
  "threadId": "...",
  "items": [{
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "PASSIVE-HANDOFF-PROBE"}]
  }]
}
```

Measured result: the request returned `{}`, emitted no turn/tool/exec
notifications, left the thread idle, and wrote the message as a `response_item`
in the rollout. This is a real non-agentic ingress, so Codex handoff no longer
needs a prompt asking the model not to act.

## Implementation

- `RuntimeCapabilities.passive_handoff` declares whether a backend can carry
  history without opening a turn. The registry rejects a backend that declares
  it but lacks `inject_context()`.
- Codex implements `inject_context()` with `thread/inject_items`. Claude declares
  no passive ingress; switching to it skips cross-runtime history and reports
  that limitation explicitly instead of running the transcript as a task.
- Transcript lookup and injection each have a 10-second ceiling. Probe already
  has a 60-second ceiling. On candidate timeout/error/cancellation, cleanup calls
  bounded `interrupt()` and `safe_disconnect()` before atomically draining
  deferred work. A failed disconnect falls back to `reconnect()` teardown.
- After adoption, incumbent retirement and the one deferred drain run in a
  shielded, awaited task, so cancellation cannot leave the chat in PROCESSING.
  No cleanup task is fire-and-forget.
- Codex tracks emitted deltas by `(turn_id, item_id)`. A completed
  `agentMessage` supplies its final text only when that specific item emitted no
  non-empty delta. Existing delta streams remain deduplicated; stale-turn items
  remain filtered.

## Verification

Focused runtime suite:

```text
108 passed, 1 skipped in 5.58s
```

The skipped test is the pre-existing opt-in live app-server isolation test.
`python -m py_compile` passed for every changed runtime module and
`git diff --check` was clean.

Eight mutations were applied independently to temporary copies. Every targeted
guard went red:

```text
agentic ingress method       exit=1
handoff timeout removed      exit=1
candidate disconnect removed exit=1
deferred drain removed       exit=1
completed fallback removed   exit=1
per-item dedupe removed      exit=1
turn scope removed           exit=1
capability validation removed exit=1
```

The rest of the available suite was split to fit the command ceiling and passed:
`302 passed, 1 skipped in 6.84s` plus `204 passed in 25.98s`. The only omitted
file, `tests/test_activity_ingress.py`, cannot be collected in this worktree
without installing the missing `aiogram_media_group` dependency, which the task
explicitly prohibited. No dependency was installed.

## Strict self-review

- The handoff path has no call to `send_message`, `turn/start`, MCP or exec.
- Timeout/error keeps the incumbent authoritative; the candidate is never
  adopted after an indeterminate injection.
- There is exactly one deferred drain in each terminal branch: abort or commit.
- Cancellation was tested both before and after candidate adoption.
- The fallback reads only normalized `agentMessage.text`; reasoning and tool
  items remain excluded.
- `thread/inject_items` is an app-server API whose schema labels the raw history
  shape as experimental. The live 0.146.0 probe proves the deployed local shape,
  while fail-loud registry validation and bounded rollback contain future drift.

No cross-LLM verdict was requested or run. This is a self-review only, per the
task constraint not to launch Claude Code/Opus.
