# upgrade-claude5 — personal notes

## Environment (cost me a turn each time)

- Test runner: `/mnt/data/Projects/Python/kesha-tg-bot/.venv/bin/python -m pytest -q`.
  No `pyproject.toml`; deps in `requirements.txt`. Venv must be on SDK **0.2.128** (prod parity) —
  on 0.1.50, 12 tests fail with `ResultMessage.__init__() got an unexpected keyword 'api_error_status'`.
  That is a stale venv, not a defect.
- `~/.config/uv/uv.toml` has `exclude-newer = "7 days"`, which hides `claude-agent-sdk==0.2.128`
  from the resolver. Install with `--exclude-newer 2030-01-01T00:00:00Z`. Do not edit the global config.
- `ClaudeSession(session_file=...)` needs a **`Path`**, not a `str` (`.exists()` is called on it).
- Prod journal timestamps are ~5 h behind the app log line. Search by content, never `--since`.

## Branch hygiene (bit me twice, in #14 and #20)

My long-lived worktree branch is based on an older branch, so `git diff origin/main` shows
thousands of deleted lines belonging to other tasks. **Never `git add -A` here.** Stage an explicit
file list, never commit `.serena/project.yml` (a tool rewrites it), and read `git diff --cached`
before committing.

## Project facts I had to dig out (kesha)

- `message_log.get_history()` is **`ORDER BY id DESC`** — newest first. Slicing `[-n:]` gives you
  the OLDEST rows. Bit me in #21.
- `role='user'` rows are NOT all user speech: fired reminders are logged as user rows with
  `message_id` **`None` or `0`** (17% of rows; a naive "last 3" is polluted 32% of the time).
  Filter on truthy `message_id` AND absence of `REMINDER FIRED` — a batch row bundles both.
- Real prod summaries live in the journal as `[kesha.compact] DEBUG Compact summary:` followed by
  untimestamped lines. Extract by "read until the next `^\d{4}-\d\d-\d\d ... [` line".
- `compact.py:_validate_summary_sections` anchors on the **LAST** `CONTINUATION` match and only
  checks duplicates in the prefix before `RECENT VERBATIM`. Text appended after the summary cannot
  break it — but verify by probe before assuming, it is #14 prod-incident code.

## Technique that keeps paying off

- **Bounding a third-party async call from outside cancels its internal cleanup.**
  `asyncio.wait_for(sdk_call(), t)` killed the SDK coroutine before its own `fail_after` handler
  popped `pending_control_responses` → one leaked entry per timeout. Use
  `wait_for(asyncio.shield(task), t)` and let the orphan finish. Measure the *resource* (dict size,
  fd count), not just the latency, before/after any timeout change.
- **Reproduce a hang with `kill -STOP` / `-CONT` on the child process.** Cheap, deterministic, and
  it reproduced a prod 60 s signature exactly — far better than reasoning about it.
- Before proposing a concurrency fix, *measure* whether concurrency is actually the cause. Here the
  obvious "a parallel turn starves the control request" story was plainly false, and a lock-based
  fix would have passed review while fixing nothing.
