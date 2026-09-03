# Task #31 verification

## Final channel and Codex 0.149 contract

The first `env_vars` implementation removed values from argv but put every MCP value in the app-server environment. It was rejected: generic Codex children can inherit that environment independently of the shell policy.

The final implementation writes literal `mcp_servers.<id>.env` entries to a per-session `CODEX_HOME/config.toml` (0600), removes every configured MCP env name from the app-server launch environment, and unlinks the config after app-server initialization but before a model turn can run. Per-session homes share only the existing rollout directory and SQLite root, preserving thread resume without sharing credential config.

Verified against upstream tag `rust-v0.149.0`, commit `758ef40f50c1a458425c7cfbf1eb12cbc07af0b0`:

- `rmcp-client/src/utils.rs:16-58` builds an MCP environment from defaults and explicit config literals.
- `rmcp-client/src/stdio_server_launcher.rs:261-289` launches the intended MCP process with `env_clear()` followed by only that constructed environment.
- `hooks/src/registry.rs:68-76,296-305` snapshots the app-server environment for a generic notify child. This proves parent-env forwarding is not containable by shell secret-name filters.
- The [official Codex configuration reference](https://developers.openai.com/codex/config-reference) defines `shell_environment_policy` as subprocess inheritance policy, but the final design does not depend on filters or credential-like names.

## Live 0.149 probe

The probe used neutral name `ORCHID_LAMP` (no `TOKEN`, `KEY`, `PASSWORD`, or `SECRET` substring) and one generated fake value. It started a real app-server, observed a model-triggered `commandExecution`, invoked a generic legacy-notify child, and started an intended stdio MCP server:

```json
{"app_server_env_has_neutral_name":false,"codex_version":"0.149.0","command_execution_observed":true,"command_execution_reported_absent":true,"credential_config_present_during_turn":false,"generic_notify_child_has_neutral_value":false,"intended_mcp_received_exact_value":true}
```

The preceding argv-specific live check on the same file-backed launch path reported `app_server_argv_has_value=false`; the automated launch regression also checks arbitrary values and neutral names against the complete argv tuple.

No production configuration or credential was read, printed, or changed.

A second live probe completed a real turn, disconnected the app-server, and reconnected through the per-session home plus shared rollout store:

```json
{"credential_config_removed_between_processes":true,"first_turn_completed":true,"mcp_received_after_reconnect":true,"shared_rollout_store_has_files":true,"thread_resumed_same_id":true}
```

## Automated checks

- `tests/test_codex_session.py`: 80 passed, 1 skipped.
- Bridge/runtime and Claude MCP-secret integration selection: 142 passed.
- The launch regression parses the 0600 config, verifies command/args and arbitrary fake values, proves argv and app-server env contain neither values nor neutral names, and spawns a generic child from the captured app-server env.
- Per-chat regression proves two sessions sharing one configured Codex root get different config homes and values.
- Invalid/reserved names and divergent cross-server values remain fail-loud.
- Concurrent connects are serialized across teardown, config write/unlink, spawn, and initialization.

Mutation check: temporarily restoring MCP values to the app-server environment made `test_mcp_secrets_reach_only_the_private_profile` fail with generic child output `present,present` instead of `absent,absent`. The mutation was removed and the focused suite re-passed.

Concurrency mutation: temporarily removing `_connect_lock` made `test_concurrent_connects_serialize_config_lifecycle` fail with two simultaneous entries (`calls == 2` instead of `1`). The mutation was removed.

## Review

Sol round 2 rejected the redesign for an unserialized config/startup lifecycle. After `_connect_lock` was applied to the full transaction and mutation-proved, round 3 returned `APPROVED` with no findings and quoted the locking line from the implementation.
