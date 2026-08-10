# #3 — Codex MCP hang and image sandbox failure

Date: 2026-08-10
Runtime under test: `codex-cli 0.146.0`, `gpt-5.6-sol`

## Production symptoms

- `mail_count_all` showed a running status for 143 seconds, then 300 seconds.
- At 300 seconds `response_stream.py` reached its tool-phase watchdog and
  abandoned the Codex stream. No `custom_tool_call_output` had arrived.
- The Mail.ru MCP child was blocked in `read(0)` with no connection to TCP 993:
  the call had not reached `server.py` or IMAP.
- `view_image` failed with
  `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`.

## Root causes

### MCP calls

A wire-level app-server probe captured the missing message:

```text
method=mcpServer/elicitation/request id=0
serverName=mailru mode=form
message=Allow the mailru MCP server to run tool "mail_count_all"?
requestedSchema={"type":"object","properties":{}}
```

`CodexSession._read_stdout()` treated every message with `method` as a
notification. It did not distinguish JSON-RPC server requests, which also have
an `id`, and therefore never wrote a response. Codex waited indefinitely before
dispatching the MCP call.

This was isolated from mail and network health: a direct
`mcpServer/tool/call` through the same app-server and Mail.ru process returned
the five folder counts in 4.2 seconds.

### Images

The VPS container cannot create the loopback interface required by Codex's
bubblewrap sandbox. A direct local comparison produced:

```text
codex sandbox -- /bin/true                              -> exit 1, RTM_NEWADDR EPERM
codex sandbox --enable use_legacy_landlock -- /bin/true -> exit 0
```

Landlock preserves the thread's `read-only` sandbox policy and avoids the
unsupported network-namespace operation. Switching to `danger-full-access` was
rejected because Kesha must not gain arbitrary filesystem writes.

## Fix

- Handle app-server JSON-RPC server requests before notification routing.
- Auto-accept only Codex's empty MCP permission form for a server already
  pinned in Kesha's MCP config.
- Decline data-bearing forms, URL elicitations, and foreign servers.
- Return JSON-RPC `-32601` for other unsupported callbacks so they fail instead
  of hanging the turn.
- Start app-server with `--enable use_legacy_landlock`; keep thread sandbox
  `read-only`.
- Render a timed-out active tool as `Прервано` with a warning marker. The old
  `ToolStatusTracker.finalize()` path converted every active tool to `Сделано`,
  which falsely presented the 300-second watchdog as a successful mail call.

## Verification

- `python3 -m pytest -q tests/test_codex_session.py tests/test_tool_status.py`:
  `51 passed, 1 skipped in 4.83s`.
- End-to-end model turn through the patched `CodexSession`, real Sol, real
  Mail.ru MCP: completed in 17 seconds and returned all folder counts.
- End-to-end image turn through the patched `CodexSession` opened
  `photo_20260810_045205_32656.jpg` and extracted the visible sleep values,
  including score 73 and duration 10 h 10 min.

## Operational note

The existing 300-second status is a watchdog timeout, not a successful tool
duration. The UI must not be interpreted as proof that the mail operation
completed when the stream ends without a tool result.
