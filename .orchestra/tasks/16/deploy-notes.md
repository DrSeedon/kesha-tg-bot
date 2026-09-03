# #16 T3–T7 — deploy notes

Prod is on `ad81cd5` (pre-T3). This covers what changes when T3–T7 ship.

## With `RUNTIME=claude` (the default): nothing changes

Verified against the real prod chat ids, not asserted:

```
RUNTIME default            : claude
registered runtimes        : ['claude', 'codex']
claude capabilities        : same object as before
session path chat 720740564: 720740564   identical=True
session path chat 893553748: 893553748   identical=True
model for claude           : from CLAUDE_MODEL, not RUNTIME_MODELS
```

- `claude_session.py` and `compact.py` are **untouched** (`git diff --stat` empty across T3–T7).
- Session filenames are byte-identical, so existing history resolves as before.
- Codex is registered but unreachable without an explicit `/runtime codex`.
- Nothing switches automatically — sticky failover is T8 and is not built.

## What is new for the user

| | |
|---|---|
| `/runtime` | shows current runtime, model, available runtimes, and live quota with a real reset date |
| `/runtime codex` / `/runtime claude` | manual switch; refused unless the chat is idle |
| limit messages | now name the subscription that is out and its reset time, instead of always saying "Claude" with no date |

A switch announces itself in chat. If the target runtime fails to start, the
bot stays on the current one and says why — no silent failover.

## Env (all optional, defaults are safe)

| var | default | effect |
|---|---|---|
| `KESHA_RUNTIME` | `claude` | startup runtime. **Leave unset on prod.** |
| `KESHA_CODEX_MODEL` | `gpt-5.6-sol` | model used when on Codex |
| `KESHA_CODEX_BIN` | resolved from PATH | codex CLI location |
| `KESHA_CODEX_HOME` | `./storage/codex-home` | private Codex config dir |

Codex needs `codex login` on the host to work at all. Until then `/runtime codex`
fails the readiness probe and refuses to switch — it does not break Claude.

## First checks after deploy

1. an ordinary message — answers as usual
2. `/status` — unchanged
3. `/runtime` — reports `claude` plus quota
4. a reminder fires — still delivered (reminders are the easiest thing to break)

Do **not** switch to Codex during the deploy. Its weekly quota is at 98% until
**08.08 12:53**; switching now would move the bot onto an exhausted runtime.

## Rollback

```
git revert --no-commit 84991b1..HEAD   # or: git checkout ad81cd5 -- <files>
systemctl restart kesha-bot-vps
```

Nothing in T3–T7 migrates data or rewrites session files, so a rollback needs no
cleanup. The only new on-disk artifacts are `storage/codex-home/` and, if Codex
was ever used, `storage/sessions/<chat>.codex` — both are ignored by the Claude
path and can be deleted.

## Known, not fixed

On the reminder path a partial answer already flushed as a separate message
survives next to a terminal limit notice, leaving a truncated fragment above it.
Pre-existing (verified against `main`), belongs to the flood fallback, tracked
separately as #19.
