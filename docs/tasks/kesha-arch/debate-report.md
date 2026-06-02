# Kesha TG Bot — Architecture Debate Report

**Date:** 2026-06-02
**Participants:** Claude Opus 4.6 + Codex GPT-5.5
**Rounds:** 3 (adversarial review → debate → consensus)
**Codebase:** 14 Python files, ~3840 LOC, flat structure in project root
**Full review:** `docs/tasks/kesha-arch/codex_architecture.md`

---

## What NOT to do (consensus — both models agree)

### Do NOT move to a Python package
Both models independently concluded that flat root layout is correct at 14 files. A package (`kesha_bot/`) adds `__init__.py`, relative imports, `python -m` entrypoint — complexity without benefit. **Migrate to package ONLY when:**
- Test suite exists (test discovery needs importable package)
- Files exceed ~25 or logical sub-packages emerge (e.g., `handlers/`, `tools/`)
- Currently: no tests, no package, no problem

### Do NOT split ChatState
626 LOC looks large but the FSM responsibilities are genuinely coupled: phase transitions, debounce, batching, inject routing, drain loop, compaction scheduling. Splitting would create MORE callback wiring between fragments, not less complexity. **Current form is the right design for the scale.**

### Do NOT add DI/service container
11 callback params in ChatRegistry constructor is ugly but pragmatic. A `Services` dataclass is a cosmetic improvement, not architectural. Enterprise DI frameworks would be over-engineering for 2 users.

### Do NOT refactor set_bot() late binding
Module-level globals via setters (`set_bot()`, `set_registry()`) are the standard aiogram pattern. Refactoring to class-based wiring is justified only with unit tests or multiple bot instances.

### Do NOT deduplicate send_* MCP tools now
5 nearly-identical send_photo/file/video/audio/voice tools. Cosmetic duplication, zero production risk. A helper would save ~60 LOC but adds indirection for tools that rarely change.

### Do NOT bring back Redis/failover
v2.1.0 removal was correct. Single VPS, 2 users, no second node. Distributed complexity is negative value.

---

## Agreed improvement plan (prioritized)

### P0 — Blocking (fix before next deploy)

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `response_stream.py:286` | `except (Exception, asyncio.CancelledError)` swallows task cancellation → turns shutdown/stop into retry/reconnect loop | Add `except asyncio.CancelledError: raise` before general `except`. Also check inner cleanup catch at line 278. |
| 2 | `kesha_tools.py:34` | `_resolve_chat()` falls back to `next(iter(ALLOWED))` → chat-bound tools can send files/reactions to wrong user (confirmed by v2.0.2 incident) | Fail-closed: return MCP error if ContextVar empty. Keep fallback only for non-sensitive ops (toggle_debug, get_bot_status). |
| 3 | `claude_session.py:215` | `inject()` not serialized. ChatState lock releases before inject call → race window = full duration of `await _client.query()`. Concurrent query on one ClaudeSDKClient is undefined behavior. | Add `asyncio.Lock` in ClaudeSession around `query()` + `_expected_results += 1` in inject(). Don't hold it around streaming send_message(). |

### P1 — Suggestion/High (fix in next patch)

| # | File | Issue | Fix |
|---|------|-------|-----|
| 4 | `chat_state.py:620` | `shutdown()` doesn't set `_shutdown=True`, doesn't await cancelled tasks, doesn't close Claude clients | Set `_shutdown=True`, cancel tasks, `await asyncio.gather(..., return_exceptions=True)`, then clear registry. |
| 5 | `reminders.py:357` | `get_lazy_block_for_prompt()` marks lazy reminders delivered BEFORE `_ask_fn` succeeds — if ask fails, reminder lost | Return `(block, ids)`, mark delivered only after successful `_ask_fn`. |
| 6 | `inbox_server.py:29` | `/inbox` accepts any `chat_id` without checking against `ALLOWED` | Reject unknown `chat_id` if `ALLOWED` is non-empty. |
| 7 | `handlers.py:270` | Voice error from Deepgram → user gets error message but voice file lost from LLM context (unlike video_note which has fallback) | Add fallback entry `[voice: path]` + error note to enqueue, matching video_note pattern. |

### P2 — Cosmetic/Low (do when touching these files)

| Issue | Status |
|-------|--------|
| ChatRegistry 11 params → dataclass `ChatDeps` | Nice-to-have, not blocking |
| `extract_text_with_urls` / `extract_caption_with_urls` → shared helper | Minor DRY, ~20 LOC saved |
| `ChatState` receives unused `set_current_chat_fn`, `work_dir` | Move to ChatRegistry only |
| `_run_batch()` prompt assembly → `build_batch_prompt()` pure function | First test candidate |
| `setup_wizard.py` — dead code, not imported | Delete or document |
| `_shutdown` field in ChatState — always False after failover removal | Keep (defensive), document |

---

## Debate highlights

### Where Codex convinced Opus (model changed position)

1. **inject() serialization** — Opus initially argued low probability (2 users, same chat rare). Codex countered: the race window equals the full duration of `await _client.query()`, not a microsecond. "Two quick messages during PROCESSING" is a normal Telegram scenario, not exotic. Fix is cheap (one Lock). **Opus accepted: blocking.**

2. **_resolve_chat() severity** — Opus recognized this as a real issue but Codex's reference to v2.0.2 incident (file sent to wrong user) elevated it from "known technical debt" to "actively dangerous in production."

### Where Opus convinced Codex (model changed position)

1. **shutdown() severity** — Codex initially rated blocking. Opus argued: after CancelledError fix, `asyncio.run()` will properly kill remaining tasks when event loop terminates. Codex agreed to downgrade to suggestion/high.

### Where both immediately agreed

- Flat structure is fine at 14 files
- ChatState FSM is acceptable (coupled responsibilities)
- CancelledError swallowing is a real bug
- set_bot() pattern is pragmatic
- compact.py, media.py, telegram_io.py are well-structured
- No enterprise patterns needed for 2-user bot

---

## Architecture verdict

**APPROVED (consensus reached)**

The architecture is sound for MVP → small production at current scale. No structural changes needed. Fix the 3 blocking async/safety issues, clean up the 4 suggestion/high items, and the codebase is solid for continued growth.
