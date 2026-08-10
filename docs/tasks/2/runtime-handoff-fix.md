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
  bounded `interrupt()` and starts `safe_disconnect()` before atomically draining
  deferred work. The switch stops waiting after 10 seconds without cancelling
  terminal disconnect escalation; `ChatState` owns the task through completion
  and retrieves its exception.
- After adoption, incumbent retirement and the one deferred drain run in a
  shielded, awaited task, so cancellation cannot leave the chat in PROCESSING.
  A slow terminal disconnect is deliberately detached from switch latency, but
  remains in the per-chat cleanup set until its done callback observes the result.
- `ClaudeSession.safe_disconnect()` atomically detaches both unique client
  owners (`_client` and `_pending_disconnect`) before awaiting SDK cleanup. A
  second cleanup therefore cannot disconnect either client twice.
- Shutdown tracks and awaits an active switch, then every detached terminal
  cleanup, before closing the current runtime and clearing the chat registry.
- Codex tracks emitted text by `(turn_id, item_id)`. A completed
  `agentMessage` supplies its final text only when that specific item emitted no
  non-empty delta or completed fallback. Existing delta streams, replayed
  completed items, and stale-turn items remain deduplicated/filtered.

## Verification

Focused runtime suite:

```text
126 passed, 1 skipped in 5.64s
```

The skipped test is the pre-existing opt-in live app-server isolation test.
`python -m py_compile` passed for every changed runtime module and
`git diff --check` was clean.

Nine mutations were applied independently and restored byte-for-byte. Every targeted
guard went red:

```text
agentic ingress method       exit=1
handoff timeout removed      exit=1
candidate terminal disconnect removed exit=1
deferred drain removed       exit=1
completed fallback removed   exit=1
per-item dedupe removed      exit=1
turn scope removed           exit=1
capability validation removed exit=1
old wait_for+reconnect cleanup restored exit=1
```

The available suite was split to fit the command ceiling and passed:
`303 passed, 1 skipped in 7.05s` plus `76 passed in 25.64s`,
`37 passed in 4.55s`, and `95 passed in 4.68s`
(511 passed, 1 skipped total). The only omitted
file, `tests/test_activity_ingress.py`, cannot be collected in this worktree
without installing the missing `aiogram_media_group` dependency, which the task
explicitly prohibited. No dependency was installed.

The mandatory external Codex review was unavailable with
`weekly_quota_upgrade_required`; the exact platform output and the explicitly
non-independent Sol self-review are recorded in `sol-self-review.md`.

## Restart runbook while Claude quota is unavailable

Runtime choice is in memory and per chat. With production's default
`KESHA_RUNTIME=claude`, a restart recreates each `ChatState` on Claude; the
persisted Claude/Codex session ids do not persist the active choice.

1. Before the restart window, confirm there is no due/missed `urgent_llm` work
   and no `storage/greet_on_restart` flag. Either can submit model work before
   the owner can issue a command.
2. After the bot's plain startup notification, send `/runtime codex` as the
   first command in every active chat. Do not send an ordinary prompt first.
3. Require the explicit `claude → codex (gpt-5.6-sol)` success message. A
   failure leaves Claude authoritative, so stop rather than sending a prompt.
4. Send `/runtime` and require `codex`, `gpt-5.6-sol`, and a live quota/reset
   value. This refreshes Codex quota without opening a model turn.
5. Send two harmless nonce prompts sequentially. Each must produce one reply,
   and the second proves the chat returned from PROCESSING to usable state.
6. Read the service log and require the switch transition back to IDLE and two
   non-zero response lengths; reject `switch ... failed`, `response 0 chars`,
   duplicate nonce text, or a chat left in PROCESSING.

Do not work around the restart window by setting `KESHA_RUNTIME=codex` in this
revision: `ChatRegistry._model_for()` returns `CLAUDE_MODEL` for the configured
startup runtime, so Codex would be built with a Claude model id. Manual switching
from the default Claude path selects `KESHA_CODEX_MODEL` correctly. Fixing startup
Codex model selection is outside #2.

## Strict self-review

- The handoff path has no call to `send_message`, `turn/start`, MCP or exec.
- Timeout/error keeps the incumbent authoritative; the candidate is never
  adopted after an indeterminate injection.
- There is exactly one deferred drain in each terminal branch: abort or commit.
- Cancellation was tested both before and after candidate adoption.
- A blocking Claude client was cancelled after adoption: the switch released
  queued work at its deadline, the one owned disconnect continued to terminal
  completion, `_client` and `_pending_disconnect` stayed empty, and its result
  was retrieved.
- Shutdown order was measured: pending candidate terminal cleanup completed
  before current-runtime disconnect, including shutdown during an active switch.
- The fallback reads only normalized `agentMessage.text`; reasoning and tool
  items remain excluded.
- `thread/inject_items` is an app-server API whose schema labels the raw history
  shape as experimental. The live 0.146.0 probe proves the deployed local shape,
  while fail-loud registry validation and bounded rollback contain future drift.

No cross-LLM verdict was requested or run. This is a self-review only, per the
task constraint not to launch Claude Code/Opus.
