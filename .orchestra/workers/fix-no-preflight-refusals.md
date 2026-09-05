# fix-no-preflight-refusals — personal notes

## `codex_review` reads project-context from the MAIN checkout, not my worktree
`codex_review` refuses with `project_context_missing` naming
`/home/kesha/projects/kesha-tg-bot/.orchestra/project-context.toml` — the main repo path.
Writing the same file into my own worktree does NOT satisfy it (tried; identical refusal).
A worker cannot fix this: the path is outside the worktree. It is an orchestrator blocker.
The refusal happens BEFORE any model run, so it costs neither a round nor an attempt — retrying
once to confirm is free.

## Full suite in this repo
```
uv run --exclude-newer 2030-01-01 --isolated --no-project --with-requirements requirements.txt \
  --with 'mcp==1.28.1' --with 'claude-agent-sdk==0.2.128' --with pytest --with pytest-asyncio \
  python -m pytest -q
```
~60 s → always `bg_create(type="run")`, never a plain Bash call. Without the two `--with` pins the
run dies on collection in `test_kesha_mcp_proxy` (newer `mcp` dropped `Server.list_tools`) — that
is not a regression of whatever I am working on. Baseline on 2026-09-05: `608 passed, 3 skipped`.

## Mutation harness that works here
`/tmp/mut.sh F OLD NEW MARK <test paths>`: `cp F F.bak` → assert the anchor occurs exactly once
(refuse to mutate blind) → replace → print marker count in the mutant → pytest → `mv F.bak F` →
`touch F`. The `touch` matters: Python invalidates `__pycache__` by (mtime, size), and a
same-length mutant keeps executing after a naive restore. Print the marker count before, in the
mutant and after restore, and say which marker it is — a production marker reads 1/1/1, a mutant
marker 0/1/0.

## Preflight admission gates live in three places, not one
`chat_state._run_batch` (incoming batch), `chat_state._do_compact` (manual `/compact`) and
`response_stream._ask_inner` (retry). They each map `check_context_reserve` reasons to a terminal
string independently. Change the policy in one and grep the other two, or the STRINGS key you
deleted still has a live reader.
