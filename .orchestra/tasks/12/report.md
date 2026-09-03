# Report — #12: Claude Opus 5

Date: 2026-07-25

## Result

Kesha was switched from `claude-opus-4-6` to `claude-opus-5` on Contabo. Existing production session files were not changed or cleared.

## Repository changes

- `requirements.txt`: `claude-agent-sdk>=0.2.128`.
- `config.py`: default model is `claude-opus-5`.
- `system_prompt.txt`: fixed-model statement names `claude-opus-5`.
- `docs/tasks/12/`: research, deployment plan, report, and retro.

Merged production revision:

```text
19a123684144da7c8d14e2ab6132ba616e451092
```

## Tests

Local:

```text
UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q
60 passed in 10.59s
```

Additional local checks:

```text
python -m compileall -q config.py
config fallback from a clean /tmp cwd: claude-opus-5
git diff --check: pass
```

Production pre-restart smoke:

```text
KESHA_NO_FILE_LOG=1 .venv/bin/python3 -c "import bot"
exit=0
```

## Deployment evidence

```text
DEPLOY_OK
sha=19a123684144da7c8d14e2ab6132ba616e451092
model=claude-opus-5
sdk=0.2.128
bundled_cli=2.1.220 (Claude Code)
service=active/running
```

Startup log:

```text
2026-07-25 11:10:47,074 [kesha] INFO Kesha bot | CWD=/opt/cog-second-brain | Model=claude-opus-5
```

Post-restart live OAuth/Agent SDK test:

```text
oauth_sdk_resume=OK
observed=claude-opus-5[1m]
context_preserved=True
```

The test created an isolated `/tmp` Opus 4.6 session, resumed it through the production venv on Opus 5, and recovered the exact prior marker. It did not read or mutate user session files.

No new startup `ERROR`, traceback, authentication, invalid-model, or usage-credit error was found after the restart.

## Rollback behavior

The first deployment attempt found that the announced merge SHA was not yet present in GitHub `origin/main`. The SHA guard failed and the deployment trap restored:

- `CLAUDE_MODEL=claude-opus-4-6`;
- `claude-agent-sdk==0.2.110`;
- active/running service state.

After the orchestrator pushed `19a1236` to `origin/main`, the same guarded procedure completed successfully. Temporary root-only backups were removed after all health and OAuth checks passed.

## Preserved state

- Production `storage/sessions` was untouched.
- The pre-existing uncommitted `claude_session.py` adaptive-thinking/high-effort hot patch remains the only dirty tracked file on Contabo.
- No proxy, MCP credential, Xray, or unrelated service configuration was changed.
- No secret values are included in this report.

## Review

Codex review was not run for the three-line runtime diff after the user explicitly requested stopping research/Codex overhead. The full pytest suite, target-host smoke test, guarded deployment, service checks, and live OAuth resume test provide the implementation evidence.

## Breaking changes and TODO

- Behavioral change: Opus 5 uses adaptive thinking by default and a newer tokenizer.
- No application API or stored-session format change.
- No required follow-up for this deployment.
