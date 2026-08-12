## Summary

The Claude-path split is functionally correct for the current six-server configuration, including only-SDK, only-external, empty, and non-dict external values. However, two argv leak paths remain, and the shared temporary filename introduces a real race/security flaw.

## Findings

- **blocking** — `claude_session.py:48`: The fixed, reusable `mcp-external.json.tmp` is unsafe. Concurrent bot/probe processes can both truncate and write the same file, producing corrupted JSON or causing one `os.replace()` to fail after the other moves it. A same-user neighboring agent can also pre-create this predictable path as a symlink or permissive file; `O_TRUNC` follows the symlink and the requested `0o600` does not repair an existing file’s mode. Use a unique same-directory temporary file created with `O_EXCL`/`mkstemp`, write and flush it, then `os.replace()`, cleaning it on failure. The current permission test only covers a fresh, uncontended path.

- **blocking** — `claude_session.py:342`: Any dictionary labeled `type == "sdk"` is copied wholesale into `options.mcp_servers`. If an SDK entry now or later contains an `env` value or another secret-bearing field, the SDK serializes that value into argv again. The assertion “They carry no secrets” is not enforced. Fail loudly if an SDK config contains `env` or other unexpected fields, or construct the inline SDK representation from an explicit safe-field allowlist while preserving `instance` for client routing. Add a test with a secret-bearing SDK entry; all six current tests remain green with this leak.

- **blocking** — `docs/tasks/19/report.md:161`: The report confirms that switching the bot to its supported Codex runtime still serializes the same MCP secrets into the app-server argv. Therefore the project-level outcome “MCP server secrets out of argv” is not achieved for a runtime users can select. Either fix both supported runtimes in this task or explicitly narrow the task and acceptance claim to the Claude runtime while tracking Codex as a blocking follow-up before it can safely be selected.

## Verdict

**REJECT** — secrets can still reach argv, and the external-config writer is vulnerable to concurrent corruption and predictable-temp-file attacks.

## Round (2026-08-12T11:10:57Z)

## Summary

All three prior blocking findings are closed.

## Findings

- **FIXED** — predictable temporary-file race and symlink attack.
- **FIXED** — SDK entries are rebuilt from an allowlist, preventing secret smuggling.
- **FIXED** — scope is clearly narrowed to Claude; Codex is explicitly excluded and warned against.
- **suggestion** — `docs/tasks/19/report.md:145`: test totals are stale (`569 passed`, six new tests) versus the stated current result of 571 passed and eight new tests.

## Verdict

**APPROVED**

Verbatim line: `temp_path = Path(temp_name)`
