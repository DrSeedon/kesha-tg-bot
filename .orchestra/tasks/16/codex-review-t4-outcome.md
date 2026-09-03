# #16 T4 — cross-LLM review: outcome

**Status: NO VERDICT. Three attempts, three timeouts. Not retried further (agreed with the orchestrator).**

## Attempts

| Job | Scope | Result |
|---|---|---|
| `bg-eee2a85f18` | T3 diff across modules | timeout, no verdict |
| `bg-bf99d9e851` | T3 diff (retry) | timeout, no verdict |
| `bg-1abb496a09` | T4 `codex_session.py`, `mode="exec"`, absolute path | timeout; **partial output contained one real finding** |
| `bg-2d23413395` | resume of the above + T3 security (`tool_bridge.py`, `file_access.py`) | timeout, no verdict |

The single-file `mode="exec"` attempt was supposed to fix the earlier
cross-module failures. It did not: the run still spent its budget echoing the
file back rather than concluding. The failure is in the review harness, not in
the artifact under review.

## What the failed review nevertheless produced

From the partial output of `bg-1abb496a09`:

> «transport не изолирует подключения/turn-события, а раннее закрытие
> генератора оставляет уведомления прошлого turn в общей очереди»

Reproduced, confirmed, fixed in `159017c`. It was a real user-visible bug:
after `/stop`, the abandoned turn's tail was consumed by the NEXT question, and
that question's own text never reached the model.

Measured before the fix:

```
turn 1 (/stop after first delta) -> consumed ['ПЕРВЫЙ-'], 3 events left queued
turn 2 (asks "ВТОРОЙ")           -> answered 'ХВОСТ-AХВОСТ-B'
```

Verified after the fix against a live app-server: `/stop` mid-list of cities,
then "capital of France" answers `Париж`, no leakage.

## Why the local tests missed it

`test_interrupt` was green. It exercised `/stop` in isolation, but the defect
lives at the SEAM between `/stop` and the following turn. A correct test of the
wrong interaction. Same shape as the earlier symlink test that validated
`resolve_sendable()` while the real leak happened later, when aiogram opened
the file.

## What stands in place of a verdict for T3

No external opinion. What exists instead:

- self-review that found the TOCTOU (`open_sendable` validates and reads in one
  step) and the hardlink escape (`st_dev`/`st_ino`, since `resolve()` cannot help)
- ~30 tests covering the path whitelist, chat addressing, argument whitelisting
  and session TTL
- empirical checks: 40 parallel sessions, 1000 expired handles, unicode
  confusables in argument names

Weaker than a second model's read. Recorded honestly rather than papered over.

## Process note

The first review's output was destroyed on our side: a `git reset --hard` at the
start of a turn removed the untracked file while the job was still writing it.
Check `bg_list` for in-flight jobs before resetting.
