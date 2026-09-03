# #24 — Codex `/compact` false success and stale reserve latch

## Read-only production evidence

Collected on 2026-08-17 without sending Telegram messages, starting another
poller, restarting the service, or changing `/opt/kesha-bot`.

- Production commit: `c1c275c5b9925fbb94aef1d04bcbcc4522c09fa8`.
- Codex CLI/app-server: `codex-cli 0.146.0`.
- Saved chat thread:
  `/opt/kesha-bot/storage/sessions/720740564.codex` =
  `019fe9bc-dbe9-7c63-bcc8-4789feeb7044`.
- Journal sequence (server-local prefix / application Krasnoyarsk timestamp):
  - 12:31:28 / 17:31:28 — next batch rejected by context reserve.
  - 12:31:31 / 17:31:31 — phase `idle -> compacting`.
  - 12:32:06 / 17:32:06 — `compact ok, 91.4% -> 91.4%`.
  - 12:43:56 / 17:43:56 — next batch rejected by reserve again.
  - 12:43:59-12:44:12 — the second compact reports the same false result.

The persisted rollout proves that Codex really compacted the thread. It did
not merely acknowledge a no-op:

| UTC | previous window | new window | window no. | replacement items |
|---|---|---|---:|---:|
| 10:32:06 | `01a00daa-...` | `01a00f47-...` | 4 | 133 |
| 10:44:12 | `01a00f47-...` | `01a00f52-...` | 5 | 133 |

Each operation wrote a top-level `compacted` record followed by
`event_msg.context_compacted`. Immediately after each compact the rollout's
token event had `last.input_tokens = 0`, with a 258400 model window. Before the
first compact, the last real turn reported exactly `236056` input tokens.

Therefore the app-server/rollout state changed correctly; the stale state was
inside `CodexSession`.

## Current app-server contract

`codex app-server generate-json-schema --experimental` on the installed 0.146.0
binary documents:

- request `thread/compact/start` with only `threadId`;
- an empty response object (not proof that background compaction completed);
- `item/completed` whose item type is `contextCompaction` as the current
  completion signal;
- deprecated `thread/compacted`, with the schema directing clients to the
  `ContextCompaction` item instead.

The adapter therefore waits for the completed item. A `turn/completed` from an
interrupted or unrelated turn is not accepted as success.

## Root cause

`CodexSession._absorb_usage()` intentionally updates the live context gauge
only when `last.inputTokens > 0`. The compact completion's zero input value did
not overwrite the last real value (`236056`). After the verified
`contextCompaction` completion, `compact_context()` returned that stale value
with `measured_after=False`.

`ChatState._do_native_compact()` then read the same cache twice, rendered
`91% -> 91%` as success, and cleared its reserve latch. The next admission
check called `CodexSession.check_context_reserve()`, which still saw only 22344
tokens free, below the 24000-token reserve, and latched the chat again before a
new turn could measure the compacted context.

## Fix and invariants

- A verified `contextCompaction` completion invalidates `_context_tokens` and
  `_last_ctx_usage`. Unknown-after-compact is honest and admits exactly one
  next turn, whose real `inputTokens` repopulates the gauge.
- Notifications queued before the compact request are stale and discarded.
  Completion requires a non-empty `item/started` id and the matching
  `item/completed` id; an unrelated completion cannot clear the gauge.
- The success message no longer invents or repeats an after percentage. It
  says the new usage will be measured after the next message.
- A timeout, app-server error, or cancellation does **not** clear the old
  gauge, because compaction was not verified. It tears down the process within
  a bounded cleanup window, preserves the thread id, and resumes it on demand.
- `ChatState` always clears compact request flags and returns to `IDLE` in its
  existing `finally` path, including cancellation.

## Verification

- Regression was red before the fix: the exact `236056/258400` fixture returned
  `context_tokens=236056` and failed before checking the next `"Ку"` reserve.
- Focused runtime/compact/reserve suite: `142 passed, 1 skipped`.
- Full suite with the production RAG dependencies plus the development pytest
  package after the review fix: `595 passed, 1 skipped in 46.03s`.
- `py_compile` passed for both changed runtime modules, config, and the two
  focused test modules.
- `git diff --check` passed.
- Mutation proof: deleting only `_context_tokens = None` made
  `test_compact_releases_precompact_high_water_before_next_message` fail with
  `assert 236056 is None` (exit 1). Restoring the line made the focused suite
  green again.
- Review-finding mutation proof: weakening the matching-id condition made
  `test_compact_requires_the_current_started_item_to_complete` fail with one
  unconsumed current completion (exit 1).
- Fresh Sol round 1 found one P1 correlation bug. It was accepted and fixed.
  Round 2 verified the matching-id guard and returned `APPROVED`, quoting the
  exact guard line from `codex_session.py`.

Review route: **Sol** (mandatory shared-runtime floor). Rounds: **2**. Verdict:
**APPROVED**. Findings: blocking 1, fixed 1; no new blockers or suggestions.
Evidence: `codex-review-impl.md` plus the named commands above. Independence:
same-family fresh reviewer session; **cross-family verdict unavailable**
because Claude/Opus was explicitly forbidden for this task.

## Pre-mortem checks

1. **Claude compaction accidentally changes.** Consumer: the Claude branch of
   `ChatState._do_compact()`. Symptom: it calls Codex native compact or loses
   the summary transaction. Covered by `test_claude_still_uses_keshas_own_compaction`
   in the focused suite.
2. **An unverified timeout clears the high-water gauge and admits an actually
   full thread.** Consumer: the next reserve check. Covered by the timeout,
   process-exit and cancellation tests, each asserting that `236056` remains
   until a completion item is observed.
3. **Cancellation leaves the chat in `COMPACTING`.** Consumer: the next
   Telegram command/message. Covered by
   `test_cancelled_native_compact_releases_the_chat`, which asserts `IDLE` and
   cleared request/start flags.
4. **Honest unknown usage regresses into another invented percentage.**
   Consumer: the Telegram success edit. Covered by
   `test_native_compact_does_not_repeat_an_unmeasured_percentage`.
5. **The compact fix admits the next message but breaks unrelated runtime or
   RAG behavior.** Nearest observable proxy: the 594-test full suite with
   production RAG dependencies.

## Review decision gate

- **Changed files and consumers:** `codex_session.py` (`compact_context`, then
  `check_context_reserve`/the next `send_message`), `chat_state.py`
  (`_do_native_compact`, `/compact` notification and phase/latch lifecycle),
  `config.py` (Telegram copy), focused tests, and this evidence document.
- **Author metadata:** Orchestra live session metadata reports
  `fix-codex-compact` on `gpt-5.6-sol` (Codex runtime); this is not inferred
  from the worker name.
- **Named acceptance criteria:** no success for an unchanged/unverified state;
  no stale high-water block after verified native compact; bounded and
  recoverable timeout/error/cancel; exact `236056 -> 236056` regression with
  the next `"Ку"` admitted; mutation proof; focused/full/compile/diff checks;
  fresh Sol review; clean `#24` commit.
- **Named oracle and observed output:**
  `pytest -q tests/test_codex_session.py tests/test_compact_dispatch.py
  tests/test_runtime_limits.py tests/test_preventive_compact.py` ->
  `141 passed, 1 skipped`; full `pytest -q` -> `594 passed, 1 skipped`;
  `py_compile` and `git diff --check` -> exit 0. The new regression was frozen
  red before implementation, then mutation-proved red independently.
- **Route:** mandatory fresh Sol technical review. The diff changes a shared
  session/runtime admission and lifecycle gate, so the high-risk floor applies
  regardless of the deterministic tests. Claude/Opus review is explicitly
  forbidden by the assignment; cross-family verdict is unavailable.
