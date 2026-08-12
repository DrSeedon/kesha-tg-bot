# #19 — MCP secrets out of argv

## Scope of the claim

This closes the leak for the **Claude runtime only** (`RUNTIME=claude`, what prod
runs). The Codex runtime still writes the same secret values into the
app-server's argv — see "Found, not fixed" below. **`/runtime codex` must not be
selected until that is fixed**; the acceptance evidence here says nothing about it.

## What was wrong

`claude_agent_sdk/_internal/transport/subprocess_cli.py:554-576` serialises
`options.mcp_servers` into argv when it is a **dict**, and `claude_session.py`
passed a dict. Every `env` block of every stdio server — including
`YOUGILE_PASSWORD` and `OPENROUTER_API_KEY` — was therefore readable by any
local process through `ps -eo args`.

Reproduced here before the fix (probe run with the fix mutated out,
`/tmp/probe_baseline.log`), against the live process tree:

```
  pid=2979768 args=24 exe=claude value_leaks=8 pattern_matches=2
    !! LEAKED VALUE of gmail.GOOGLE_OAUTH_CLIENT_ID
    !! LEAKED VALUE of gmail.GOOGLE_OAUTH_CLIENT_SECRET
    !! LEAKED VALUE of websearch.OPENROUTER_API_KEY
    !! LEAKED VALUE of yougile.YOUGILE_API_KEY
    !! LEAKED VALUE of yougile.YOUGILE_BASE_URL
    !! LEAKED VALUE of yougile.YOUGILE_COMPANY_ID
    !! LEAKED VALUE of yougile.YOUGILE_EMAIL
    !! LEAKED VALUE of yougile.YOUGILE_PASSWORD
argv value leaks: 8
```

## The constraint that shapes the fix

`_internal/client.py:140-146` collects in-process SDK servers by iterating the
dict and reading `config["instance"]`:

```python
sdk_mcp_servers = {}
if configured_options.mcp_servers and isinstance(configured_options.mcp_servers, dict):
    ...
    sdk_mcp_servers[name] = config["instance"]
```

A `str`/`Path` fails `isinstance(..., dict)`, so `sdk_mcp_servers` comes out
empty and all 16 Kesha tools disappear. "Just pass the file path" is therefore
not available.

## Measured: the CLI merges repeated `--mcp-config`

Not assumed — run against the real binary. Two flags, one inline JSON and one
file, both servers present in the `system/init` event:

```
$ claude -p "ok" --output-format stream-json --verbose --strict-mcp-config \
    "--mcp-config={\"mcpServers\":{\"probeInline\":{...}}}" --mcp-config=a.json
  mcp_servers: [{"name": "probeInline", "status": "failed"},
                {"name": "probeFileA",  "status": "failed"}]
```

(`failed` only because the probe servers were `/bin/cat`; presence is the point.)

Two syntax notes that cost a run each:

- `--mcp-config` is declared variadic (`<configs...>`), so the two-token form
  swallows the following subcommand: `--mcp-config a.json mcp list` tries to
  open `./mcp` and `./list` as config files. The `=` form binds correctly, and
  it is the form the SDK's `extra_args` uses for dash-leading values only —
  our path is plain, so the SDK emits the two-token form, which is fine because
  nothing follows it that could be mistaken for a config.
- `claude mcp list` ignores `--mcp-config` entirely; it reports user-scope
  servers. It is not a probe for this question.

## The change

`claude_session.py::_make_options` splits the dict:

- `type == "sdk"` entries (only `kesha`, which carries no secrets) stay in
  `options.mcp_servers` so the SDK keeps routing their tool calls;
- everything else is written to `storage/mcp-external.json` (0600, atomic
  replace) and handed over as `extra_args={"mcp-config": <abs path>}`.

The path is absolute because the CLI resolves it against **its own** cwd
(`WORK_DIR=/opt/cog-second-brain`), not the bot's.

## Acceptance

**1. No secret values in argv.** The ticket's own command, run against the live
tree (5 processes: bot, CLI, gmail/mailru/yougile MCP children):

```
$ tr '\0' '\n' < /proc/<pid>/cmdline | grep -nEi "password|secret|token|key|y0_|sk-or-v1-|ya29\.|AIza"
--- pid 2982776 (bot) ---                       (no output)
--- pid 2982856 (claude CLI) ---
11:- Media files ... /home/kesha/orchestra/worktrees/.../fix-argv-secrets/...
23:- NEVER send plain unformatted text walls ... at least *bold* for key...
109:/home/kesha/orchestra/worktrees/.../fix-argv-secrets/storage/mcp-ex...
--- pid 2982912 (gmail) ---                     (no output)
--- pid 2982911 (mailru) ---                    (no output)
--- pid 2982910 (yougile) ---                   (no output)
```

All three matches are false positives of the pattern, not secrets: two are the
system prompt (the word "key" in a formatting rule, and the media path), one is
the `--mcp-config` path. All three match only because this **worktree is named
`fix-argv-secrets`** — in prod the path is `/opt/kesha-bot/storage/...` and the
pattern finds nothing there.

Because the ticket's pattern cannot distinguish those, the probe also matches
the **exact live values** of all 8 secrets against every argv in the tree:

```
argv value leaks total: 0
```

Same probe, same command, fix mutated out → 8. The check demonstrably reddens.

**2. All six servers.** `MCP servers loaded: ['kesha', 'yougile', 'mailru',
'gmail', 'websearch', 'ozon']`, and every external one actually spawned its
child process under the CLI (`node` for websearch, `ssh` for ozon, three
pythons) — which is the positive proof that the file config is honoured, not
merely accepted.

**3. Live tool calls** (`/tmp/probe_fixed.log`):

```
  [tool] mcp__kesha__get_bot_status
  [tool] mcp__websearch__search
```
```
Model: claude-opus-5
Session: 272fe835-655c-4b0d-bdc9-2cbe5c37cb9b
CWD: /opt/cog-second-brain
Rate limit: allowed (five_hour)
Context: 5% (45372/1000000)
```
```
_perplexity/sonar | 3→18 tokens | $0.0050_
The capital of France is **Paris**.
```

In-process SDK server and a secret-consuming external server both work.

**4. Tests.** `571 passed, 1 skipped` (563 on main + 8 new), full run, no `-x`.
Two of the eight were added after the Codex review; the six below are the
original set.
New file `tests/test_mcp_config_secrets.py` asserts on the argv the SDK
actually builds (`SubprocessCLITransport._build_command()`), not on our options
object — the leak lived in the serialisation step, so a test stopping at
`options.mcp_servers` would stay green while `ps` still showed the password.
Mutation: fix removed → 3 of 6 red; restored → 6 green.

**5. Repository artifacts.** Form-based scan (`y0_`, `sk-or-v1-`, `ya29.`,
`gh[pousr]_`, `AIza`, `Bearer <25+>`) over all 205 tracked files on this
branch: no matches. Independently, all 8 live secret values were substring-
matched against every tracked file: none present. `storage/` is gitignored
(`.gitignore:9`), so the new config file cannot be committed.

## Why the bot was not started for real

A second poller would fight the live `kesha-bot-vps` for `getUpdates`. The probe
(`docs/tasks/19/probe_argv.py`) runs the real wiring instead — `bot._load_global_mcp()`,
a real `ChatRegistry`, `ChatRegistry.build_session()`, a real CLI spawn and real
tool calls — and skips only aiogram polling and the RAG/inbox startup, none of
which touch MCP config.

## Found, not fixed (owner's call)

1. **The Codex runtime leaks the same values.** `codex_session.py:461-463`
   renders `-c mcp_servers.<name>.env.<KEY>=<value>` into the app-server's
   argv. Verified, not inferred:

   ```
   leaks in codex argv: ['mcp_servers.websearch.env.OPENROUTER_API_KEY="sk-or-v1-LEAKCANARY"']
   ```

   Not fixed here: the natural fix is to move server definitions into the
   private `CODEX_HOME` config that `_ensure_codex_home()` already writes, but
   `tests/test_codex_session.py:203` pins `"mcp_servers" not in config` ("private
   config must not define servers"), and this role may not modify existing
   tests. Needs its own ticket with that assertion revisited. Only reachable
   when `RUNTIME=codex`; prod is `claude`.

2. **The source file is world-readable.** The secrets do not come from
   `~/.claude.json` (that one has no `mcpServers` at all) but from
   `/opt/cog-second-brain/.mcp.json`, mode **0644**. Any local user — including
   agents of neighbouring projects — can read it directly, which is the same
   exposure this ticket closed for argv. It is gitignored in the COG-second-brain
   repo (`.gitignore:39`), so it has not reached GitHub. `chmod 600` is a
   one-liner but the file is outside this project, so it is left to the owner.

## Codex review (round 1 → fixes)

Artifact: `docs/tasks/19/codex-review-impl.md`. Verdict REJECT, three blocking.
Two were real and are fixed; the third is a scope question, answered above.

1. **Predictable temp file** (`claude_session.py:48`). The fixed
   `mcp-external.json.tmp` was both a race between chats and a symlink target any
   same-user process could pre-create — `O_TRUNC` follows it, and mode 0600 is
   not applied to a file that already exists. Fixed with `tempfile.mkstemp` in the
   destination directory plus `os.replace`, the idiom `_write_session_id` already
   uses. Confirmed by mutation, not by reading: restoring the old writer makes
   `test_write_does_not_follow_a_planted_symlink` fail on
   `assert victim.read_text() == "do not touch"` — the planted symlink really was
   followed and the victim file really was clobbered.

2. **SDK entry copied wholesale** (`claude_session.py:342`). The comment claimed
   "they carry no secrets" without enforcing it; the SDK strips only `instance`,
   so any other key on an SDK config lands in argv. Now the inline entry is
   rebuilt from exactly the three fields anyone reads (`type`, `name` for the CLI,
   `instance` for SDK routing). Mutation: copying wholesale again turns
   `test_sdk_entry_cannot_smuggle_a_secret_into_argv` red.

3. **Codex runtime not covered.** Accepted as stated, answered by narrowing the
   claim (top of this file) rather than by widening the change: the fix needs
   `tests/test_codex_session.py:203` revisited, and this role may not modify
   existing tests.

## Order of operations

Values are now out of argv. **Rotation comes next and is the owner's** —
`YOUGILE_PASSWORD`, `YOUGILE_API_KEY`, `OPENROUTER_API_KEY`,
`GOOGLE_OAUTH_CLIENT_SECRET` were exposed to every local process for as long as
the bot has been running, and neighbouring agents ingested them twice today.
