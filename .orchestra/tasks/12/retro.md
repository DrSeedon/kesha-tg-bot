# Retro — #12 (Claude Opus 5)

## Metrics

- Tool calls: 40+ | Retries: 2 focused retries (default fallback test, deployment after missing remote push) | Turns: multiple live corrections | Files: 7 task files
- Codex: cancelled/skipped by explicit user instruction | Tests: 60 passed; production smoke and OAuth resume passed
- User/orchestrator corrections this task: 3 (exact Opus 5 target; remove full-cycle overhead; stop Codex/research waiting)

## What went wrong (signal → root cause)

- **Signal:** the target was corrected from the preliminary Fable alternative to exact `claude-opus-5`. **Root cause:** same-day model information changed between search results, while the initial task framing anchored on an alternative name before the refreshed official model table was checked. **Category:** process.
- **Signal:** the user explicitly rejected repeated research/Codex/sleep overhead for a three-line configuration change. **Root cause:** pipeline ceremony was not compressed after the exact ID and live OAuth access were already proven. **Category:** scope.
- **Signal:** the first fallback-default assertion failed. **Root cause:** `env -u CLAUDE_MODEL` was insufficient because `load_dotenv()` reloaded the repository `.env`; the test needed a clean working directory. **Category:** correctness.
- **Signal:** the first guarded deployment rolled back because `origin/main` was still `7882522`, despite a reported merged SHA. **Root cause:** the orchestrator merge existed locally but had not been pushed; remote SHA was checked only after `git pull`, inside the rollback transaction. **Category:** process.
- **Signal:** `claude doctor` timed out after 60 seconds with no output. **Root cause:** a broad updater diagnostic was unnecessary once exact CLI and SDK OAuth requests were available. **Category:** scope.

## What went well (keep doing)

- The deployment SHA guard and rollback trap prevented an unmerged revision from being deployed and restored the old model/runtime automatically.
- The supported SDK was tested from `/tmp` before production mutation; this caught the difference between “old client answers a smoke request” and Anthropic's documented minimum.
- The final production verification checked configured model, actual SDK-observed model, resume context, service state, and sanitized journal errors without exposing command-line secrets.
- The pre-existing dirty `claude_session.py` and all production session files were preserved.

## Proposed changes (Tier-2 — NOT applied, awaiting approval)

| Target | Change | Evidence | Status |
|---|---|---|---|
| Deployment workflow | Verify `git ls-remote origin refs/heads/main` equals the expected merged SHA before creating a mutation/rollback transaction. | First deploy attempt rolled back solely because the merge was not pushed (n=1). | logged, not promoted |
| Full-cycle pipeline | Allow a fast path for ≤3 literal configuration changes after primary-source and live-target verification, without background Codex review. | Explicit user correction about research/Codex/sleep overhead (n=1). | logged, not promoted |

## Written to worker memory (Tier-1 — applied)

- none
