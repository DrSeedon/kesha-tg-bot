# fix-argv-secrets — personal notes

## Running tests on the VPS (cost me two dead ends)

There is **no `.venv` in the repo checkout or in a worktree** on this VPS. The only
project venv is the prod one, `/opt/kesha-bot/.venv` — and it has **no pytest**.
Orchestra's venv has pytest but SDK 0.2.114, below the 0.2.128 prod parity the suite
needs. Working combination, no installs anywhere:

```bash
env -u VIRTUAL_ENV TELEGRAM_BOT_TOKEN="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" \
    HOME=/home/kesha PYTHONPATH=/opt/kesha-bot/.venv/lib/python3.12/site-packages \
    /home/kesha/orchestra/.venv/bin/python -m pytest tests/ -q
```

PYTHONPATH wins over the venv's own site-packages, so app deps come from prod
(0.2.128) and pytest from Orchestra. `TELEGRAM_BOT_TOKEN` must be a
**format-valid dummy** or `Bot()` raises at import — `<digits>:<35 chars>` is enough,
the constant above is verified to pass. Never borrow prod `.env` into a worktree.

## Do not start the bot to "test it live"

`main()` polls Telegram with the prod token → fights `kesha-bot-vps` for `getUpdates`,
and also binds the inbox port and kicks off a RAG backfill. To exercise real wiring,
build a `ChatRegistry` by hand and use `registry.get(chat).session` — that is the
production path minus polling. Pattern kept in `docs/tasks/19/probe_argv.py`.

## Secret checks: match VALUES, never name patterns

`grep -Ei "password|secret|token|key"` over argv is worthless here — it fires on the
system prompt, on `ssh -o StrictHostKeyChecking`, and on any path that happens to
contain the word (my own worktree was named `fix-argv-secrets`, which matched three
times). Read the real values out of the config and substring-match those; report only
variable NAMES.

## Where this project's MCP secrets actually live

`/opt/cog-second-brain/.mcp.json` — **not** `~/.claude.json` (that one has no
`mcpServers` key at all). `bot._load_global_mcp()` merges three sources and the third
is the one that matters. Check the source before reasoning about the config.
