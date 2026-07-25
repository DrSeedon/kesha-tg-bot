# Research — #12: upgrade Kesha to Claude Opus 5

Date: 2026-07-25

## Question

- **Context:** Kesha runs on Contabo (`kesha-bot-vps`, user `kesha`, `/opt/kesha-bot`) through Python `claude-agent-sdk`, which launches a bundled Claude Code CLI under the user's Claude Code OAuth session.
- **Change under test:** replace production `CLAUDE_MODEL=claude-opus-4-6` with the newly released Claude Opus 5.
- **Baseline:** Opus 4.6, persistent per-chat session IDs, 1M model suffix, adaptive thinking/high effort currently present as an uncommitted production hot patch.
- **Outcome:** the official model ID must be confirmed by primary sources; the exact model must answer through the current OAuth account and a supported Agent SDK/CLI; resuming an Opus 4.6 session on Opus 5 must retain context; the production service must remain rollback-safe.

## Hypotheses considered

### H1 — `claude-opus-5` is the correct target and is usable by Kesha

Claude Opus 5 is the requested model because Anthropic released that exact model and ID on 2026-07-24, and the Contabo OAuth account is entitled to run it.

- **Falsifier:** official live documentation does not list `claude-opus-5`, or a live OAuth request returns an invalid-model, entitlement, credit, or authentication error.

### H2 — the current production SDK is already a supported runtime for Opus 5

The existing `claude-agent-sdk==0.2.110` might be sufficient because direct calls with the full model ID can bypass picker-level version gating.

- **Falsifier:** Anthropic documents a newer minimum Claude Code version than the SDK bundle, even if a trivial request happens to succeed.

### H3 — switching the explicit model preserves persistent session semantics

Passing `model="claude-opus-5[1m]"` together with an existing Opus 4.6 `resume` session ID changes the active model without clearing conversation history.

- **Falsifier:** the resumed session reports Opus 4.6, loses a marker from the prior turn, creates an error result, or requires deleting production session files.

## Findings

### 1. The exact requested model exists

**CONFIRMED — two first-party Anthropic sources.**

- Anthropic's live model table lists **Claude Opus 5**, Claude API ID and alias `claude-opus-5`, 1M context, 128k maximum output, adaptive thinking, and default effort `high` on Claude API and Claude Code [1].
- Anthropic announced Claude Opus 5 on **2026-07-24** and states that it was available that day [2].
- The initial alternative that no literal “Opus 5” existed is **REFUTED**. Fable 5 is not the target after the orchestrator's explicit correction.

### 2. Opus 5 is available through the Contabo OAuth account

**CONFIRMED — direct live measurements on the target host.**

Environment used: Contabo `158.220.127.161`, user `kesha`, no proxy variables, fresh `/tmp` sessions, no production session IDs.

Direct installed Claude CLI (`2.1.197`) measurements:

```text
requested=claude-opus-5      exit=0 is_error=False result=MODEL_ACCESS_OK
model_usage_keys=['claude-haiku-4-5-20251001', 'claude-opus-5']

requested=claude-opus-5[1m]  exit=0 is_error=False result=MODEL_ACCESS_OK
model_usage_keys=['claude-haiku-4-5-20251001', 'claude-opus-5[1m]']
```

These calls prove that the current OAuth subscription recognizes and serves both the exact ID and the form Kesha currently constructs with `[1m]`. They do not by themselves prove that the installed CLI version is officially supported.

### 3. The installed SDK/CLI is below Anthropic's supported minimum

**CONFIRMED — first-party version requirement plus direct version measurements.**

- Claude Code documentation says Opus 5 requires **Claude Code 2.1.219 or later** [4].
- Production measurements:

```text
global Claude Code:                 2.1.197
claude-agent-sdk:                   0.2.110
SDK bundled Claude Code:            2.1.191
```

- Therefore H2 is **REFUTED**: successful trivial requests on 2.1.191/2.1.197 are useful counter-evidence against an immediate hard failure, but both runtimes remain below the documented support floor.
- The SDK uses its bundled CLI before the system CLI unless `ClaudeAgentOptions.cli_path` is explicitly set; the official SDK README documents both bundling and the `cli_path` override [5]. Kesha does not set `cli_path`, so upgrading only `/usr/bin/claude` would not upgrade the bot runtime.

### 4. `claude-agent-sdk==0.2.128` provides a supported bundled CLI and works with Kesha's option shape

**CONFIRMED — live package-registry lookup, wheel inspection, and target-host experiment.**

Raw measurements:

```text
PyPI JSON latest version:                    0.2.128
0.2.128 wheel bundled Claude Code:           2.1.220
official Opus 5 minimum:                     2.1.219
```

A temporary, non-production install of `claude-agent-sdk==0.2.128` on Contabo was loaded ahead of the production environment and tested with:

- `model="claude-opus-5[1m]"`
- `thinking={"type": "adaptive"}`
- `effort="high"`
- current OAuth credentials
- an Opus 4.6 session created in `/tmp`, then resumed on Opus 5

Raw result:

```text
sdk=0.2.128
bundled_cli=2.1.220 (Claude Code)
requested=claude-opus-5[1m]
observed=claude-opus-5[1m]
context_preserved=True
is_error=False
```

The Agent SDK changelog between installed 0.2.110 and the later published series contains CLI bumps and bug/security fixes rather than a documented Python API breaking change relevant to Kesha's used surface [6]. The repository requirement `claude-agent-sdk>=0.1.50` already permits 0.2.128, although the deployment must explicitly upgrade the existing venv because `git pull` alone does not reinstall dependencies.

### 5. Persistent sessions survive the model change

**CONFIRMED — 3/3 pre-declared resume trials on the target host.**

Pass criteria were declared before the experiment: each resumed run must report Opus 5, return the exact marker supplied to the preceding Opus 4.6 turn, and finish without an error result.

Using the currently installed SDK/runtime with isolated `/tmp` sessions:

```text
iteration=1 old_observed=claude-opus-4-6[1m] new_observed=claude-opus-5[1m] context_preserved=True new_is_error=False
iteration=2 old_observed=claude-opus-4-6[1m] new_observed=claude-opus-5[1m] context_preserved=True new_is_error=False
iteration=3 old_observed=claude-opus-4-6[1m] new_observed=claude-opus-5[1m] context_preserved=True new_is_error=False
```

The supported 0.2.128 runtime was then tested separately and also preserved the marker. No production session file was read, changed, or deleted.

H3 is confirmed for the exact SDK option pattern used by Kesha.

### 6. The model migration is API-compatible with Kesha's current thinking configuration

**CONFIRMED — first-party migration guide plus live 0.2.128 test.**

Anthropic's Opus 4.6 → Opus 5 guide directs changing `claude-opus-4-6` to `claude-opus-5`; Opus 5 retains 1M context, adaptive thinking, prompt caching, vision, MCP and client-side tools [3].

Relevant behavior changes:

- manual `thinking: {"type": "enabled", "budget_tokens": ...}` is rejected;
- adaptive thinking is on by default;
- `thinking={"type": "adaptive"}` is valid;
- default effort is `high`;
- Opus 5 uses a newer tokenizer and may count roughly 1.0–1.35× as many tokens as pre-4.7 Opus models [3].

Kesha does not configure manual extended thinking or non-default sampling parameters. Production has an existing uncommitted `claude_session.py` hot patch that passes adaptive thinking and high effort; the exact combination succeeded in the 0.2.128 experiment.

### 7. Current production state and the minimal configuration surface are known

**CONFIRMED — direct code and service inspection.**

```text
service ActiveState=active
service SubState=running
configured CLAUDE_MODEL=claude-opus-4-6
running SDK subprocess model=claude-opus-4-6[1m]
last startup log: Kesha bot | CWD=/opt/cog-second-brain | Model=claude-opus-4-6
```

Code path:

- `config.py` loads `CLAUDE_MODEL` from `/opt/kesha-bot/.env` via `load_dotenv()`.
- `bot.py` passes the configured model to every lazy-created `ClaudeSession` and logs `Model=<value>` during startup.
- `claude_session.py` appends `[1m]` and passes both `model` and any persisted `resume` ID to `ClaudeAgentOptions`.
- `system_prompt.txt` separately hardcodes “Model is fixed to claude-opus-4-6”; it must be changed to Opus 5 to avoid a false self-description.

## Counter-evidence and limitations

- **Older runtimes answered successfully.** This argues that the model endpoint does not universally reject pre-2.1.219 clients. It does not override Anthropic's explicit support requirement, and older clients may lack Opus 5 picker/fallback behavior or later protocol fixes.
- **`[1m]` is redundant.** Opus 5 has a 1M context by default [1][3], but both CLI and SDK live tests accept Kesha's existing `claude-opus-5[1m]`. Removing the suffix would broaden the change without a demonstrated benefit.
- **Native web fetch is unavailable on Opus 5.** Anthropic lists this as an exception [3]. Kesha has an external MCP web-search server, so this is not a startup blocker, but prompts that specifically rely on Claude's native WebFetch tool can behave differently.
- **Safety classifiers can refuse some cyber/biology prompts.** Current Claude Code documents automatic fallback behavior and ties Opus 5 support to 2.1.219+ [4]. This strengthens the case for upgrading the bundled CLI before production use.
- **Production worktree is dirty.** `/opt/kesha-bot/claude_session.py` contains an uncommitted adaptive-thinking/high-effort hot patch. It is relevant and compatible, but it must be preserved; deployment must not reset or overwrite it.
- **Reproducibility limitation.** `requirements.txt` only has `claude-agent-sdk>=0.1.50` and is outside this worker's owned directories. An explicit production venv upgrade to 0.2.128 is possible without editing it, but pinning a minimum in the repository would require orchestrator authorization for that shared file.
- `claude doctor` on the old global CLI produced no output and timed out after 60 seconds. Direct OAuth queries and SDK tests succeeded, so this does not block the model migration; it does mean `doctor` is not a useful health signal here.

## Affected files and deployment state

Owned repository files that may need changes after approval:

- `system_prompt.txt` — replace the old hardcoded model name.
- `config.py` — only if the plan decides the environment fallback should match production; the live service uses `.env`, so changing the fallback is not required for the deployed switch.
- `CHANGELOG.md` — record the production model/runtime upgrade if project release policy requires it.
- `docs/tasks/12/*` — plan, reviews, report, and possible retro.

External deployment state explicitly in task scope:

- `/opt/kesha-bot/.env` — change only `CLAUDE_MODEL`, preserving every other value.
- `/opt/kesha-bot/.venv` — upgrade `claude-agent-sdk` from 0.2.110 to 0.2.128 before restart.
- `kesha-bot-vps` — controlled restart followed by startup-log, active/running, journal-error, and actual SDK subprocess model checks.

Must not be touched:

- production session files;
- the unrelated dirty `claude_session.py` hot patch;
- proxies or Xray;
- any secret values in logs or task artifacts.

## Risks and edge cases for the plan

1. **Rollback must cover both axes.** If Opus 5 fails after restart, restore `CLAUDE_MODEL=claude-opus-4-6` and `claude-agent-sdk==0.2.110`, restart, and verify the old model.
2. **Do not use broad process/status output.** The current Claude subprocess command line contains MCP configuration. Health checks must extract only state/model fields and must never write full argv or environment values to reports.
3. **Preserve `.env` atomically.** Back up the file with root-only permissions, replace only the exact `CLAUDE_MODEL` line, and validate it without printing other variables.
4. **Validate imports before restart.** Run the project smoke import with the upgraded venv and no secret-bearing output.
5. **Inspect actual model, not only config.** Startup log proves configured model; a sanitized subprocess/model or first SDK initialization signal must prove the launched model is `claude-opus-5[1m]`.
6. **Persistent session verification should be non-destructive.** Existing session files remain in place; the already completed isolated experiments are the migration proof. Production verification must not inject test turns into user chats.

## Conclusion

`claude-opus-5` is the correct and available target. The OAuth account, 1M suffix, adaptive/high settings, and persistent resume behavior are compatible. The current Agent SDK bundle is below Anthropic's documented minimum, so the safe production path is to upgrade the venv to `claude-agent-sdk==0.2.128` (bundled CLI 2.1.220), change only `CLAUDE_MODEL` plus the stale system-prompt model name, restart under rollback protection, and verify both service health and the actual launched model.

## Sources

1. **Primary:** Anthropic, “Models overview” — https://platform.claude.com/docs/en/about-claude/models/overview
2. **Primary:** Anthropic, “Introducing Claude Opus 5” (2026-07-24) — https://www.anthropic.com/news/claude-opus-5
3. **Primary:** Anthropic, “Migration guide,” section “Migrating to Claude Opus 5 from Claude Opus 4.6 and earlier Opus models” — https://platform.claude.com/docs/en/about-claude/models/migration-guide
4. **Primary:** Anthropic, “Claude Code model configuration” — https://code.claude.com/docs/en/model-config
5. **Primary source code/docs:** Anthropic Claude Agent SDK for Python README — https://github.com/anthropics/claude-agent-sdk-python
6. **Primary source code:** Anthropic Claude Agent SDK Python changelog — https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/CHANGELOG.md
7. **Primary package registry:** PyPI live project JSON — https://pypi.org/pypi/claude-agent-sdk/json
