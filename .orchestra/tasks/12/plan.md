# Plan — #12: switch Kesha to Claude Opus 5

## Goal

Deploy `claude-opus-5` on Contabo with an officially supported Agent SDK bundle, preserve existing chat session IDs, and restore Opus 4.6 automatically if any pre-restart or post-restart check fails.

## Assumptions

- The orchestrator explicitly approved Phase 2/3 without another idle gate.
- The target is strictly `claude-opus-5`; Fable must not appear in configuration or deployment.
- The existing uncommitted production `claude_session.py` adaptive/high hot patch belongs to the user and must remain untouched.
- The production OAuth account is valid; isolated CLI and SDK tests already returned Opus 5.

## Changes

### Repository

- `requirements.txt`
  - Raise the minimum from `claude-agent-sdk>=0.1.50` to `claude-agent-sdk>=0.2.128`.
  - Reason: 0.2.128 bundles Claude Code 2.1.220; Anthropic documents 2.1.219 as the Opus 5 minimum.
- `config.py`
  - Change only the `CLAUDE_MODEL` fallback from `claude-sonnet-4-6` to `claude-opus-5`.
  - Production still reads the explicit value from `.env`.
- `system_prompt.txt`
  - Change only the fixed-model self-description from Opus 4.6 to Opus 5.
- `docs/tasks/12/*`
  - Keep research, plan, Codex reviews, report, and retro if triggered.

### Production

1. Preflight:
   - require `kesha-bot-vps` to be active/running;
   - record the current git revision, `CLAUDE_MODEL`, SDK version, and bundled CLI version without printing unrelated environment values;
   - require the only pre-existing tracked modification to remain `claude_session.py`;
   - create root-only backups of `.env` and the three repository files changed by this task.
2. Pull the orchestrator-merged commit as user `kesha`.
3. Upgrade only `claude-agent-sdk` in `/opt/kesha-bot/.venv` to the requirement-resolved version and verify that it is at least 0.2.128 and its bundled CLI is at least 2.1.219.
4. Replace exactly one `CLAUDE_MODEL=` line in `/opt/kesha-bot/.env` with `CLAUDE_MODEL=claude-opus-5`; fail before restart if the key is absent or duplicated.
5. Run the required `python -c "import bot"` smoke test with the production venv.
6. Restart `kesha-bot-vps` once.
7. Verify:
   - `ActiveState=active`, `SubState=running`;
   - startup journal contains `Model=claude-opus-5`;
   - no new `ERROR`, traceback, authentication, invalid-model, or credit error appears after the restart;
   - an isolated production-venv SDK test reports `claude-opus-5[1m]` and retains a marker across an Opus 4.6 → Opus 5 resume.
8. On any failure after mutation:
   - restore `.env` and changed repository files from root-only backups;
   - reinstall `claude-agent-sdk==0.2.110`;
   - restart;
   - verify `Model=claude-opus-4-6` and active/running;
   - report the exact sanitized blocker.

## What not to touch

- Production session files under `storage/sessions`.
- The dirty production `claude_session.py`.
- MCP configs, credentials, proxy settings, Xray, Telegram state, or user conversations.
- `.env` values other than `CLAUDE_MODEL`.
- README/CHANGELOG or unrelated model references.

## Verification

Local:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q
python -m compileall -q config.py
KESHA_NO_FILE_LOG=1 env -u CLAUDE_MODEL python -c 'import config; assert config.MODEL == "claude-opus-5"'
rg -n 'claude-(opus-4-6|fable-5)|claude-agent-sdk>=' requirements.txt config.py system_prompt.txt
```

Production checks must print only the selected model, package versions, service state, and sanitized journal lines.

## Tickets

### T1 — Make the repository select a supported Opus 5 runtime

- Files: `requirements.txt`, `config.py`, `system_prompt.txt`, `docs/tasks/12/*`
- AC:
  - `requirements.txt` requires `claude-agent-sdk>=0.2.128`.
  - Unset `CLAUDE_MODEL` resolves to `claude-opus-5`.
  - The system prompt names only `claude-opus-5`.
  - No task-owned runtime configuration contains `claude-fable-5`.
  - Local pytest/compile/import checks pass, or absence of tests/tooling is recorded with a narrower successful check.
- blocked-by: none

### T2 — Deploy, restart, verify, and retain rollback

- Files/state: `/opt/kesha-bot/.env`, `/opt/kesha-bot/.venv`, `kesha-bot-vps`; report in `docs/tasks/12/report.md`
- AC:
  - Merged task commit is present on Contabo.
  - Production SDK is at least 0.2.128 and bundled CLI is at least 2.1.219.
  - `.env` contains exactly one `CLAUDE_MODEL=claude-opus-5`.
  - Smoke import succeeds before restart.
  - Service is active/running after one controlled restart.
  - New startup log reports `Model=claude-opus-5`.
  - Isolated production-venv SDK request reports Opus 5 and resume context is preserved.
  - No new startup/auth/model/credit error is present.
  - Existing `claude_session.py` modification and session files remain intact.
- blocked-by: T1
