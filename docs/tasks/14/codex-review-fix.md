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
