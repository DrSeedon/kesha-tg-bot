🧯 The permanent latch was fixed by adding another permanent latch. Two release-blocking defects remain.

## Summary

Answers to the five questions:

- **a)** Yes. `usage_limit_active` becomes permanently `True`: admission refuses every later turn, while the only clearing path requires a successful later turn. `/clear` does not clear it. The model-output latch has a similar reachability problem, although `/clear` resets that one.
- **b)** Clearing the latch from a genuinely good payload is reasonable: admission also validates fresh context usage. Alternating payloads remain last-write-wins, but ordinary turns cannot reach the next good payload after a bad one because admission is already blocked.
- **c)** Yes. The expected model remains config-scoped while requests are session-scoped. Per-session overrides and `use_1m=False` are broken.
- **d)** User braces are safe, but the new localized runtime-invariant message deterministically raises `KeyError("expected")`.
- **e)** Empty quota usage should be non-evidence, but the branch is too broad: a non-empty map containing only the wrong model is also treated as non-evidence. A wrong context model is caught separately; a max-output-only mismatch can remain admitted with warnings.

## Findings

### [blocking] `usage_limit_active` permanently seals admission

[claude_session.py:572](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:572) returns before every subsequent query while `usage_limit_active` is true. The only normal clearing assignment is after a successful non-error terminal response at [claude_session.py:475](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:475), but admission can no longer produce that response. Even `/clear` leaves the flag unchanged. Consequently, one plan-limit response blocks that `ClaudeSession` until process restart or out-of-band mutation.

The ordering before `_max_output_tokens_valid` is semantically reasonable—an active quota is the more immediate cause—but only after the quota state becomes expiring or probeable. As written, it also permanently masks a simultaneous runtime-invariant failure. The added test at `tests/test_claude_session_limit.py:541-543` misses this because it merely asserts that the result is not `runtime_invariant`; it accepts the permanently blocking `usage_limit` result.

---

### [blocking] Runtime-invariant notification raises `KeyError`

[chat_state.py:173](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/chat_state.py:173) calls `_t_cfg(entry.message, key)` without `fmt`. That helper already executes `.format(**kw)` at [config.py:199](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/config.py:199), so `context_runtime_invariant`, which contains `{expected}`, raises `KeyError` before the new formatting at lines 174–175 runs. This affects the normal Telegram-message path; the `entry.message is None` branch works.

Pass `fmt` into `_t_cfg` and do not format twice:

```suggestion
            text = _t_cfg(entry.message, key, **fmt)
            await entry.message.answer(text, parse_mode=None)
```

Other currently selected terminal keys contain no placeholders and remain safe. User text is never used as the format template, and braces inside the substituted `expected` value are not reparsed.

---

### [suggestion] The “good payload clears the latch” recovery is unreachable normally

[claude_session.py:393](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:393) can restore `_max_output_tokens_valid`, but after a contradictory result sets it false, [claude_session.py:575](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:575) rejects the next ordinary turn before it can generate that good payload. Recovery currently requires `/clear`, an already-in-flight result, or bypassing normal admission. The new test does exactly that bypass at `tests/test_claude_session_limit.py:581`: it calls `collect()` directly rather than exercising `check_context_reserve → send_message`.

This does not create a fail-open; it means the advertised automatic recovery is ineffective. Either make recovery an explicit `/clear` contract or add an independent revalidation path.

---

### [suggestion] Derive the invariant from each session’s actual model

[claude_session.py:46](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:46) resolves the invariant from global `config.MODEL`, while [claude_session.py:259](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:259) resolves the request from `self.model` and `self.use_1m`. These are demonstrably allowed to differ: the constructor default is Sonnet while the config default is Opus.

With a per-session override, the client requests the override but `get_context_usage()` is compared against the config model at line 611, so every message is rejected as `runtime_invariant`. Terminal usage for the session model is also missed because line 387 looks up the config model. Use `resolve_context_model(self.model, self.use_1m)` consistently for options, terminal validation, reserve validation, logging, and response metadata—or explicitly prohibit session overrides.

---

### [suggestion] Distinguish absent usage from contradictory usage

[claude_session.py:398](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:398) treats all of these identically:

- `model_usage={}` from a verified quota result;
- a non-empty map containing only another model;
- an expected-model entry that is malformed or lacks `maxOutputTokens`.

Only the first is established non-evidence. A normal payload containing other model keys but not the expected model is affirmative evidence of drift and should latch false. The cheapest robust change is to classify `result_is_limit` before invariant handling, tolerate empty usage only for known short-circuit results, and log/latch non-empty wrong-model maps including their keys. For ordinary empty successful results, at least expose a counter or health alert; strict fail-closed semantics would reject after such an unverifiable result.

A full model/1m downgrade is still caught by the fresh control invariant at lines 606–614, so it is not silent. The remaining blind spot is primarily max-output verification when terminal usage stays absent.

---

### [suggestion] Handle the new reasons consistently outside normal admission

The new `usage_limit` result at [claude_session.py:572](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:572) is mapped correctly for normal batches at [chat_state.py:648](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/chat_state.py:648), but manual `/compact` still maps it to `context_unknown` at `chat_state.py:780-788`. Also, the live control-invariant return at `claude_session.py:624-628` omits `expected_model`, causing the new message to display `(?)`. Both undermine the stated goal of naming the real cause.

## Verdict

**❌ Changes requested.** The original empty-usage corruption is fixed, and clearing on positively good evidence does not itself weaken the live fail-closed check. However, quota recovery is now impossible through normal admission, and the primary runtime-mismatch notification crashes with `KeyError`; both must be fixed before deployment.

The safety gate currently behaves like a nightclub bouncer who rejects everyone forever, then loses the guest list when asked why. 🎟️

## Round (2026-08-01T07:28:39Z)

🧪 The obvious traps are gone; naturally, quota classification still happens one block too late. Both prior blockers are fixed, but one quota shape can recreate the permanent admission outage.

## Summary

Prior findings:

- ✅ `usage_limit_active` no longer gates reserve admission. A retry reaches Claude, and a successful terminal clears it at [claude_session.py:495](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:495). Quota remains accurately surfaced as `kind="usage_limit"` and handled by [response_stream.py:503](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:503).
- ✅ The formatting `KeyError` is fixed.
- ✅ Runtime validation consistently uses `self.expected_context_model`; the module constant has no remaining production comparison.
- ✅ Live invariant failures now include `expected_model`.
- ⚠️ Non-empty quota usage can still poison `_max_output_tokens_valid`; details below.
- ⚠️ The earlier “good payload recovery is unreachable through normal admission” observation remains true.

Every `check_context_reserve` rejection path:

- Connection failure, lines 581–591: no latch; transient failures recover on retry. Invalid persisted sessions require `/clear`.
- `_max_output_tokens_valid=False`, lines 598–610: persistent admission latch. Cleared by reset, session-candidate reset, or a good terminal payload—but ordinary admission cannot produce that payload while blocked.
- Control-request failure, lines 612–622: no latch; retry recovers. Missing session requires `/clear`.
- Non-dict usage, lines 624–625: no latch; next request probes again.
- Invalid live usage, lines 629–652: no latch; automatically recovers when a later control payload is valid.
- Insufficient reserve, lines 654–661: `ChatState` intentionally latches `_context_reserve_blocked`; successful `/compact` or `/clear` clears it.

The per-session property stays consistent during replacement because replacement never changes `model` or `use_1m`. One scope caveat: `use_1m=False` still cannot work with the fixed one-million-token constants and 208K reserve; treat the reserve subsystem as explicitly 1M-only unless that mode is meant to be supported.

## Findings

### [blocking] Classify quota before mutating the runtime latch

[claude_session.py:395](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:395) validates `model_usage` before `result_is_limit` is calculated at [claude_session.py:446](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:446). A real quota result can contain partial or auxiliary usage—for example, only the legitimate Haiku entry—after an `AssistantMessage` or `RateLimitEvent` has already established the limit. That non-empty map omits Opus, so line 422 latches false; the same result is then correctly classified as quota, but every later admission stops at line 598. Determine `result_is_limit` first and leave the invariant unchanged for any positively identified quota/short-circuit terminal, regardless of whether its usage map is empty.

---

### [suggestion] Preserve source-side validation evidence across rollback

`begin_session_replacement` snapshots the latch before the source-summary request. That request can set or clear it at [claude_session.py:403](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:403), but rollback blindly restores the older snapshot at [claude_session.py:218](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:218). A failed compact can therefore erase a newly discovered contradiction—or discard newly proven-good source evidence. Update the snapshot’s validation fields immediately before `start_session_candidate`; candidate failures should restore the latest source state, not the pre-summary state.

---

### [question] Choose explicit recovery semantics for a real contradiction

[claude_session.py:598](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:598) still prevents a subsequent normal turn from reaching the “good payload clears the latch” code. `/clear` reliably recovers, and automatic compact may bypass admission, but ordinary retry cannot. If contradiction recovery is intentionally operator-driven, the comment and direct-`collect()` test overpromise automatic recovery. If automatic recovery is required, it needs an independent probe rather than a later user turn.

---

### [suggestion] Complete runtime-invariant messaging on retry paths

The initial batch and manual compact now report `runtime_invariant`, but the retry preflight at [response_stream.py:408](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:408) still maps it to `context_unknown`. Also, [config.py:100](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/config.py:100) says “different model,” although the same reason covers wrong context size, max output, token count, or enabled auto-compact. Pass `expected_model` through the retry handler and describe a broader runtime-configuration mismatch.

## Verdict

**❌ Changes requested.** The two accepted blockers are correctly resolved, and model identity is consistent across ordinary and replacement sessions. Before deployment, quota results must be classified before terminal-usage validation; otherwise a benign non-empty short-circuit payload can weld the admission gate shut again.

Same gate, fresher paint, one quota-shaped welding torch still lying nearby. 🔥
