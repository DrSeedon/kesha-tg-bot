<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-sol"} -->

ACK — `child_env: dict[str, str] = {}`

> ⚠ Codex usage unaccounted: ValueError: Codex completed turn reported zero tokens

## Round (2026-08-23T16:51:34Z)

<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-sol"} -->

## Summary

Re-review status: prior app-server environment secret exposure is FIXED. One new startup/config lifecycle race remains.

## Findings (Conventional Comments)

- [blocking] NEW BUG — Serialize `_connect()` with the existing `_connect_lock`. Concurrent callers can both pass `is_alive`, then teardown each other’s process and overwrite/unlink the shared per-session `config.toml` while the other app-server is initializing. This can start an MCP-less process, revoke the wrong bridge session, or attach reader tasks to a replaced `_proc`. The new temporary credential-config lifecycle makes this race security- and startup-critical. [codex_session.py](/home/kesha/orchestra/worktrees/home-kesha-projects-kesha-tg-bot/fix-codex-argv-secrets/codex_session.py:578)

## Verdict

REJECTED — blocking config lifecycle/startup race.

## Round (2026-08-23T16:54:53Z)

<!-- codex-review-metadata: {"reviewer_model": "gpt-5.6-sol"} -->

## Summary

Re-review status: FIXED. The lock covers the complete connection/config lifecycle, and current callers do not re-enter `_connect()` while it is held.

Exact changed line: `async with self._connect_lock:`

## Findings

None.

## Verdict

APPROVED
