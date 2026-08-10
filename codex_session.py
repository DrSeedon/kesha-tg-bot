"""Codex runtime — ChatRuntime adapter over `codex app-server --stdio`.

Second runtime for Kesha (task #16): when the Claude subscription is banned or
throttled, the bot keeps answering on a Codex/GPT subscription.

Shape borrowed from Orchestra's app/backend_codex.py: one app-server process
owns one resumable thread, JSON-RPC over stdio, notifications translated into
the chunk contract response_stream.py already speaks.

Deliberate differences from Orchestra's backend, all verified against a live
app-server (docs/tasks/16/spikes/turn_probe_events.jsonl):

* No `turn/steer`. Mid-turn injection is out of scope for #16 — Kesha replaced
  injection with a deferred queue in #14, so `mid_turn_inject=False` is honest.
* MCP servers are pinned to exactly what the caller passes. A bare app-server
  inherits the user's global ~/.codex/config.toml servers (the probe pulled in
  serena/kwin/orchestra), which would hand Kesha's chat tools it must not have.
* Context accounting comes from `thread/tokenUsage/updated`, which reports
  `last.inputTokens` and `modelContextWindow` — real numbers, not estimates.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from runtime_protocol import RuntimeCapabilities

logger = logging.getLogger(__name__)

# ChatGPT-auth effective window. Authoritative value arrives per turn in
# `thread/tokenUsage/updated.modelContextWindow`; this is only the bootstrap
# guess used before the first turn reports.
DEFAULT_CONTEXT_WINDOW = 258_400

# Reserve mirrors claude_session.py: enough headroom for a full answer plus the
# incoming prompt, or the compact floor when the user asked for /compact.
NORMAL_TURN_RESERVE_TOKENS = 24_000
MANUAL_COMPACT_FLOOR_TOKENS = 12_000

CONNECT_TIMEOUT_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 180
INTERRUPT_TIMEOUT_SECONDS = 5
COMPACT_TIMEOUT_SECONDS = 300

# Terminal Codex error classes. `usageLimitExceeded` is the subscription limit
# we must never retry; `contextWindowExceeded` needs a compact, not a reconnect.
_USAGE_LIMIT_INFOS = ("usageLimitExceeded", "sessionBudgetExceeded")
_CONTEXT_LIMIT_INFOS = ("contextWindowExceeded",)


def _codex_binary() -> str:
    """Resolve the codex CLI. The npm global bin is not always on PATH."""
    explicit = os.getenv("KESHA_CODEX_BIN")
    if explicit:
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    fallback = Path.home() / ".npm-global" / "bin" / "codex"
    return str(fallback)


def _format_reset(resets_at: Optional[int]) -> str:
    """Render a unix reset timestamp in Krasnoyarsk local time."""
    if not resets_at:
        return ""
    try:
        from datetime import datetime, timedelta, timezone

        krsk = timezone(timedelta(hours=7))
        return datetime.fromtimestamp(int(resets_at), tz=krsk).strftime("%d.%m %H:%M")
    except Exception:
        return ""


class CodexProtocolError(RuntimeError):
    """JSON-RPC error returned by the Codex app-server."""

    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', 'Codex app-server error')}")


class CodexSession:
    """ChatRuntime backed by a persistent `codex app-server --stdio` process."""

    CAPABILITIES = RuntimeCapabilities(
        # #14 removed mid-turn injection from every ingress path; Codex would
        # support turn/steer, but Kesha does not use it.
        mid_turn_inject=False,
        # thread/compact/start is native and keeps the thread id.
        native_compact=True,
        # A percentage exists only after a turn reports token usage. Before the
        # first turn the context is genuinely unknown, so this stays False —
        # an unknown context must never masquerade as a live gauge.
        context_percentage=False,
        # Subscription auth reports no dollar cost.
        cost_reporting=False,
        # thread/resume restores the thread across bot restarts.
        resume_across_restart=True,
    )

    def __init__(
        self,
        cwd: str,
        model: str = "gpt-5.6-sol",
        system_prompt: str = "",
        mcp_servers: Optional[dict] = None,
        session_file: Optional[Path] = None,
        on_connecting=None,
        reasoning_effort: str = "medium",
    ):
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt
        self.mcp_servers = mcp_servers or {}
        self._session_file = session_file
        self._on_connecting = on_connecting
        self.reasoning_effort = reasoning_effort

        self.session_id: Optional[str] = self._load_session()

        # Parity attributes read by chat_state/handlers regardless of runtime.
        self.last_cost_usd: Optional[float] = None
        self.total_cost_usd: float = 0.0
        self.last_usage: Optional[dict[str, Any]] = None
        self.rate_limit: Optional[dict[str, Any]] = None
        self.usage_limit_active = False
        self.last_duration_ms: int = 0
        self.last_num_turns: int = 0
        self.last_stop_reason: Optional[str] = None

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue[dict] = asyncio.Queue()
        self._request_seq = 0
        self._write_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._connected = False
        self._active_turn_id: Optional[str] = None
        self._last_stderr = ""

        self._context_tokens: Optional[int] = None
        self._context_window = DEFAULT_CONTEXT_WINDOW
        self._last_ctx_usage: Optional[dict] = None

    # ---------- session persistence ----------

    def _load_session(self) -> Optional[str]:
        if self._session_file and self._session_file.exists():
            sid = self._session_file.read_text().strip() or None
            if sid:
                logger.info(f"Codex: loaded thread {sid[:8]}...")
                return sid
        return None

    def _save_session(self) -> None:
        if not self._session_file:
            return
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._session_file.with_suffix(self._session_file.suffix + ".tmp")
        tmp.write_text(self.session_id or "")
        os.replace(tmp, self._session_file)

    # ---------- transport ----------

    @property
    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.returncode is None and self._connected)

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        stream = self._proc.stdout
        while True:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # Fail soft on an oversized frame rather than killing the reader.
                logger.warning(f"Codex: oversized stdout frame skipped: {exc}")
                continue
            except Exception as exc:
                logger.warning(f"Codex: stdout read failed: {exc}")
                break
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                future = self._pending_requests.pop(msg["id"], None)
                if future and not future.done():
                    future.set_result(msg)
            elif "id" in msg and msg.get("method"):
                await self._handle_server_request(msg)
            elif msg.get("method"):
                self._notifications.put_nowait(msg)

        # Process died — unblock every waiter instead of hanging on a future.
        self._connected = False
        for future in list(self._pending_requests.values()):
            if not future.done():
                future.set_exception(
                    RuntimeError(f"Codex app-server exited: {self._last_stderr[-300:]}")
                )
        self._pending_requests.clear()
        self._notifications.put_nowait({"method": "_process/exited", "params": {}})

    async def _handle_server_request(self, msg: dict) -> None:
        """Answer app-server callbacks so a turn cannot wait forever on stdin.

        Codex 0.146 asks the client to approve every model-initiated MCP call
        through ``mcpServer/elicitation/request`` even with
        ``approvalPolicy=never``.  Kesha previously treated that JSON-RPC
        request as a notification, so neither native MCP calls nor code-mode's
        nested ``tools.mcp__...`` calls ever reached the configured server.

        Only the empty permission form generated by Codex itself is accepted,
        and only for an MCP server already present in Kesha's pinned config.
        Real elicitations (forms requesting data, URLs, or foreign servers) are
        declined rather than filled in or left hanging.
        """
        request_id = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}

        if method == "mcpServer/elicitation/request":
            server = str(params.get("serverName") or "")
            message = str(params.get("message") or "")
            schema = params.get("requestedSchema")
            permission_prefix = f'Allow the {server} MCP server to run tool "'
            is_empty_permission = (
                params.get("mode") == "form"
                and schema == {"type": "object", "properties": {}}
                and message.startswith(permission_prefix)
                and message.endswith('"?')
            )
            accepted = server in self.mcp_servers and is_empty_permission
            result: dict[str, Any] = {
                "action": "accept" if accepted else "decline",
            }
            if accepted:
                result["content"] = {}
            await self._write_rpc_response(request_id, result=result)
            logger.info(
                "Codex: MCP permission %s for configured server %s",
                "accepted" if accepted else "declined",
                server or "<missing>",
            )
            return

        logger.warning("Codex: unsupported server request %s", method)
        await self._write_rpc_response(
            request_id,
            error={"code": -32601, "message": f"Unsupported server request: {method}"},
        )

    async def _write_rpc_response(
        self,
        request_id: Any,
        *,
        result: Optional[dict] = None,
        error: Optional[dict] = None,
    ) -> None:
        if not (self._proc and self._proc.stdin):
            return
        payload = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        async with self._write_lock:
            self._proc.stdin.write(json.dumps(payload).encode() + b"\n")
            await self._proc.stdin.drain()

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            try:
                line = await self._proc.stderr.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                self._last_stderr = f"{self._last_stderr}\n{text}"[-2000:]
                logger.debug(f"Codex stderr: {text}")

    async def _request(self, method: str, params: dict, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict:
        if not (self._proc and self._proc.stdin):
            raise RuntimeError("Codex app-server is not running")
        self._request_seq += 1
        request_id = self._request_seq
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            async with self._write_lock:
                self._proc.stdin.write(payload.encode() + b"\n")
                await self._proc.stdin.drain()
            msg = await asyncio.wait_for(future, timeout=timeout)
        except BaseException:
            self._pending_requests.pop(request_id, None)
            raise
        if "error" in msg:
            raise CodexProtocolError(method, msg["error"])
        return msg.get("result") or {}

    async def _notify(self, method: str, params: dict) -> None:
        if not (self._proc and self._proc.stdin):
            return
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        async with self._write_lock:
            self._proc.stdin.write(payload.encode() + b"\n")
            await self._proc.stdin.drain()

    def _ensure_codex_home(self) -> str:
        """Build a private CODEX_HOME so Kesha inherits no foreign MCP servers.

        Measured (docs/tasks/16/spikes/mcp_isolation_probe.py): `-c mcp_servers={}`
        MERGES into the user's ~/.codex/config.toml and leaves serena, kwin,
        orchestra and openaiDeveloperDocs running inside Kesha's thread. Only a
        separate CODEX_HOME drops that table. Auth is symlinked, so the user's
        subscription login still applies without copying credentials around.
        """
        home = Path(
            os.getenv("KESHA_CODEX_HOME")
            or Path(self._session_file.parent if self._session_file else Path("./storage"))
            / "codex-home"
        ).expanduser()
        home.mkdir(parents=True, exist_ok=True)
        try:
            home.chmod(0o700)
        except OSError:
            pass

        config = home / "config.toml"
        desired = f'model = {_toml_str(self.model)}\n'
        if not config.exists() or config.read_text() != desired:
            config.write_text(desired)

        self._link_auth(home)
        return str(home)

    @staticmethod
    def _link_auth(home: Path) -> None:
        """Point Kesha's private home at the user's real credentials.

        A symlink resolves by PATH, so it keeps working when `codex login`
        rewrites, atomically replaces, or unlinks-and-recreates auth.json
        (all three verified). The one broken state is a dangling link — after
        a logout, or if the target moved — so it is re-pointed every connect
        instead of only at creation, and a missing target is reported loudly
        rather than surfacing later as an unexplained 401.
        """
        auth_link = home / "auth.json"
        user_home = Path(os.getenv("KESHA_CODEX_USER_HOME", Path.home() / ".codex"))
        user_auth = user_home / "auth.json"

        if auth_link.is_symlink() and auth_link.readlink() != user_auth:
            auth_link.unlink()
        if auth_link.is_symlink() and not auth_link.exists():
            # Dangling: the target went away (logout). Drop it so a later
            # login is picked up instead of silently failing to authorize.
            auth_link.unlink()

        if not auth_link.exists():
            if not user_auth.exists():
                logger.warning(
                    f"Codex: no credentials at {user_auth} — run `codex login` "
                    "or Kesha's Codex runtime will not authorize"
                )
                return
            try:
                auth_link.symlink_to(user_auth)
            except OSError as exc:
                logger.warning(f"Codex: could not link auth.json: {exc}")

    def _mcp_config_args(self) -> list[str]:
        """Pin MCP servers to exactly what Kesha passes.

        `--disable apps` removes the built-in `codex_apps` server, which is a
        feature flag rather than a config entry and therefore survives a private
        CODEX_HOME. Together these leave the thread with only Kesha's bridge.
        """
        args: list[str] = [
            "--disable",
            "apps",
            # This VPS cannot create the loopback interface required by
            # bubblewrap (RTM_NEWADDR EPERM).  Codex's legacy Landlock backend
            # preserves the read-only policy without that network-namespace
            # operation; verified with codex-cli 0.146.0.
            "--enable",
            "use_legacy_landlock",
        ]
        for name, cfg in self.mcp_servers.items():
            command = cfg.get("command") if isinstance(cfg, dict) else None
            if not command:
                continue
            args += ["-c", f"mcp_servers.{name}.enabled=true"]
            args += ["-c", f"mcp_servers.{name}.command={_toml_str(str(command))}"]
            srv_args = cfg.get("args") or []
            rendered = ", ".join(_toml_str(str(a)) for a in srv_args)
            args += ["-c", f"mcp_servers.{name}.args=[{rendered}]"]
            env = cfg.get("env") or {}
            for key, value in env.items():
                args += ["-c", f"mcp_servers.{name}.env.{key}={_toml_str(str(value))}"]
        return args

    async def _connect(self) -> None:
        """Start the app-server and attach to a thread (resume when possible)."""
        if self.is_alive:
            return
        if self._on_connecting:
            try:
                self._on_connecting()
            except Exception:
                pass

        await self._teardown_process()

        cmd = [_codex_binary(), "app-server", "--stdio", *self._mcp_config_args()]
        env = dict(os.environ)
        env["CODEX_HOME"] = self._ensure_codex_home()
        logger.info(f"Codex: starting app-server (model={self.model})")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            limit=16 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                await self._request(
                    "initialize",
                    {"clientInfo": {"name": "kesha", "title": "Kesha", "version": "1"}},
                )
                await self._notify("initialized", {})
                await self._start_or_resume_thread()
        except BaseException:
            await self._teardown_process()
            raise
        self._connected = True

    async def _start_or_resume_thread(self) -> None:
        params: dict[str, Any] = {
            "cwd": self.cwd,
            "model": self.model,
            "approvalPolicy": "never",
            # Kesha is a chat assistant: it must not edit the filesystem. Its
            # real abilities arrive through the tool bridge, which does its own
            # path validation (file_access.py).
            "sandbox": "read-only",
        }
        if self.system_prompt:
            params["developerInstructions"] = self.system_prompt

        requested = self.session_id
        if requested:
            try:
                result = await self._request("thread/resume", {**params, "threadId": requested})
            except Exception as exc:
                # A thread that cannot be resumed must not silently become a new
                # one: the user would lose history without being told.
                logger.warning(f"Codex: resume of {requested[:8]} failed: {exc}")
                raise RuntimeError(f"session_unavailable: {exc}") from exc
        else:
            result = await self._request("thread/start", params)

        thread_id = (result.get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server returned no thread id")
        if requested and thread_id != requested:
            raise RuntimeError(
                f"Codex resumed a different thread: requested={requested}, got={thread_id}"
            )
        if thread_id != self.session_id:
            self.session_id = thread_id
            self._save_session()
        logger.info(f"Codex: thread {thread_id[:8]}... ready")

    async def _teardown_process(self) -> None:
        self._connected = False
        self._active_turn_id = None
        proc = self._proc
        self._proc = None
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
        while not self._notifications.empty():
            try:
                self._notifications.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ---------- streaming ----------

    async def send_message(self, text: str) -> AsyncGenerator[dict, None]:
        """Run one Codex turn, yielding response_stream's chunk contract."""
        try:
            await self._connect()
        except Exception as exc:
            message = str(exc)
            kind = "session_unavailable" if "session_unavailable" in message else None
            yield {"type": "error", "content": message, **({"kind": kind} if kind else {})}
            return

        if not self.session_id:
            yield {"type": "error", "content": "Codex thread is not initialized"}
            return

        started = time.monotonic()
        self.last_stop_reason = None
        try:
            result = await self._request(
                "turn/start",
                {
                    "threadId": self.session_id,
                    "input": [{"type": "text", "text": text}],
                    "model": self.model,
                },
            )
        except Exception as exc:
            yield self._error_chunk_from_exception(exc)
            return

        turn_id = (result.get("turn") or {}).get("id")
        self._active_turn_id = turn_id

        try:
            async for chunk in self._consume_turn(turn_id):
                yield chunk
        finally:
            # The caller can abandon this generator mid-turn (user /stop breaks
            # out of the `async for`). Anything still queued belongs to a turn
            # nobody is listening to, and would otherwise be read as the NEXT
            # turn's answer. Verified: without this, the tail of a stopped
            # reply is emitted as the reply to the following question.
            self._discard_turn_events(turn_id)

        self.last_duration_ms = int((time.monotonic() - started) * 1000)
        self.last_num_turns += 1

    def _discard_turn_events(self, turn_id: Optional[str]) -> None:
        """Drop queued notifications belonging to a finished/abandoned turn."""
        kept: list[dict] = []
        while not self._notifications.empty():
            try:
                msg = self._notifications.get_nowait()
            except asyncio.QueueEmpty:
                break
            params = msg.get("params") or {}
            event_turn = params.get("turnId") or ((params.get("turn") or {}).get("id"))
            # Process-level events are not turn-scoped and must survive.
            if msg.get("method") == "_process/exited":
                kept.append(msg)
            elif turn_id and event_turn and event_turn != turn_id:
                kept.append(msg)
        for msg in kept:
            self._notifications.put_nowait(msg)

    async def _consume_turn(self, turn_id: Optional[str] = None) -> AsyncGenerator[dict, None]:
        """Translate notifications until the turn settles.

        The app-server reports a failure twice — once as a standalone `error`
        notification and again inside `turn/completed.turn.error` (verified in
        the probe). Only the terminal `turn/completed` is surfaced, so the user
        never sees one failure as two messages.
        """
        pending_error: Optional[dict] = None
        while True:
            try:
                msg = await self._notifications.get()
            except asyncio.CancelledError:
                raise
            method = msg.get("method") or ""
            params = msg.get("params") or {}

            # Ignore anything stamped for a different turn. Without this a
            # stale `turn/completed` from an abandoned turn would end this one
            # before the model had said anything.
            if turn_id and method != "_process/exited":
                event_turn = params.get("turnId") or ((params.get("turn") or {}).get("id"))
                if event_turn and event_turn != turn_id:
                    continue

            if method == "_process/exited":
                self._active_turn_id = None
                yield {
                    "type": "error",
                    "content": f"Codex app-server exited: {self._last_stderr[-300:]}",
                }
                return

            if method == "item/agentMessage/delta":
                delta = params.get("delta") or ""
                if delta:
                    yield {"type": "text_delta", "content": delta}
                continue

            if method == "item/started":
                chunk = self._tool_chunk(params.get("item") or {})
                if chunk:
                    yield chunk
                continue

            if method == "item/completed":
                # agentMessage text already streamed as deltas; re-yielding it
                # would duplicate the answer (response_stream ignores `text`
                # after deltas, but staying silent keeps the contract clean).
                continue

            if method == "thread/tokenUsage/updated":
                self._absorb_usage(params.get("tokenUsage") or {})
                continue

            if method == "account/rateLimits/updated":
                self._absorb_rate_limits(params.get("rateLimits") or {})
                continue

            if method == "error":
                pending_error = params.get("error") or {}
                continue

            if method == "turn/completed":
                self._active_turn_id = None
                turn = params.get("turn") or {}
                error = turn.get("error") or pending_error
                if turn.get("status") == "failed" or error:
                    yield self._error_chunk(error or {})
                    return
                self.usage_limit_active = False
                self.last_stop_reason = "end_turn"
                yield {"type": "turn_done"}
                return

            if method == "turn/failed":
                self._active_turn_id = None
                yield self._error_chunk(params.get("error") or pending_error or {})
                return

    def _tool_chunk(self, item: dict) -> Optional[dict]:
        """Map a started item to a `tool` chunk for the status bubble."""
        item_type = item.get("type") or ""
        if item_type == "mcpToolCall":
            server = item.get("server") or ""
            tool = item.get("tool") or ""
            name = f"mcp__{server}__{tool}" if server else tool
            return {"type": "tool", "name": name, "input": item.get("arguments") or {}}
        if item_type == "commandExecution":
            return {
                "type": "tool",
                "name": "Bash",
                "input": {"command": item.get("command") or ""},
            }
        if item_type == "webSearch":
            return {"type": "tool", "name": "WebSearch", "input": {"query": item.get("query") or ""}}
        if item_type == "fileChange":
            return {"type": "tool", "name": "FileChange", "input": {"changes": item.get("changes") or []}}
        return None

    def _error_chunk(self, error: dict) -> dict:
        info = error.get("codexErrorInfo")
        message = str(error.get("message") or "Codex error")
        if isinstance(info, str) and info in _USAGE_LIMIT_INFOS:
            self.usage_limit_active = True
            return {"type": "error", "content": self._usage_limit_text(message), "kind": "usage_limit"}
        if isinstance(info, str) and info in _CONTEXT_LIMIT_INFOS:
            return {"type": "error", "content": message, "kind": "context_limit"}
        lowered = message.lower()
        if "usage limit" in lowered or "rate limit" in lowered or "quota" in lowered:
            self.usage_limit_active = True
            return {"type": "error", "content": self._usage_limit_text(message), "kind": "usage_limit"}
        if "context window" in lowered or "too long" in lowered:
            return {"type": "error", "content": message, "kind": "context_limit"}
        return {"type": "error", "content": message}

    def _error_chunk_from_exception(self, exc: Exception) -> dict:
        if isinstance(exc, CodexProtocolError):
            return self._error_chunk(exc.error)
        return self._error_chunk({"message": str(exc)})

    def _usage_limit_text(self, message: str) -> str:
        """Attach the real reset time so the user is not told to guess."""
        primary = (self.rate_limit or {}).get("primary") or {}
        when = _format_reset(primary.get("resetsAt"))
        if when:
            return f"{message} (сброс {when})"
        return message

    def _absorb_usage(self, usage: dict) -> None:
        window = usage.get("modelContextWindow")
        if isinstance(window, int) and window > 0:
            self._context_window = window
        last = usage.get("last") or {}
        total = usage.get("total") or {}
        self.last_usage = last or None
        # `last.inputTokens` is what the model actually carried into this call —
        # that IS the live context size. `total` accumulates across the thread
        # and must never be read as current context (Orchestra shipped that bug).
        tokens = last.get("inputTokens")
        if isinstance(tokens, int) and tokens > 0:
            self._context_tokens = tokens
            self._last_ctx_usage = {
                "totalTokens": tokens,
                "maxTokens": self._context_window,
                "percentage": round(tokens / self._context_window * 100, 1),
                "model": self.model,
            }
        if isinstance(total.get("totalTokens"), int):
            self.last_usage = {**(self.last_usage or {}), "threadTotalTokens": total["totalTokens"]}

    def _absorb_rate_limits(self, limits: dict) -> None:
        self.rate_limit = limits or None
        if limits.get("rateLimitReachedType"):
            self.usage_limit_active = True

    # ---------- context accounting ----------

    async def check_context_reserve(
        self,
        combined: str = "",
        *,
        manual: bool = False,
    ) -> dict:
        """Admit a turn only when the thread demonstrably has headroom.

        Codex reports context only after a turn runs, so before the first turn
        the size is unknown. Unknown is treated as `ok` here deliberately: a
        fresh thread cannot be full, and failing closed on no evidence would
        block Kesha's very first Codex message.
        """
        required = (
            MANUAL_COMPACT_FLOOR_TOKENS
            if manual
            else NORMAL_TURN_RESERVE_TOKENS + _estimate_tokens(combined)
        )
        try:
            await self._connect()
        except Exception as exc:
            message = str(exc)
            reason = "session_unavailable" if "session_unavailable" in message else "unknown"
            logger.warning(f"Codex context reserve connect failed ({reason}): {exc}")
            return {"ok": False, "reason": reason, "required": required}

        if self._context_tokens is None:
            # No turn has reported usage yet — nothing proves the thread is full.
            return {"ok": True, "reason": None, "required": required, "remaining": None}

        remaining = self._context_window - self._context_tokens
        return {
            "ok": remaining >= required,
            "reason": None if remaining >= required else "reserve",
            "remaining": remaining,
            "required": required,
            "usage": self._last_ctx_usage,
        }

    async def get_context_usage(
        self,
        *,
        refresh: bool = False,
        preserve_session: bool = False,
    ) -> Optional[dict]:
        if refresh:
            try:
                await self._connect()
            except Exception as exc:
                logger.warning(f"Codex get_context_usage refresh failed: {exc}")
                return self._last_ctx_usage
        return self._last_ctx_usage

    async def read_quota(self) -> Optional[dict]:
        """Ask the app-server for the current subscription limits.

        Cheap and side-effect free, so it doubles as the readiness probe when
        switching runtimes: a process that starts but cannot answer this is not
        actually usable, and announcing a switch to it would strand the user.
        """
        await self._connect()
        limits = await self._request("account/rateLimits/read", {}, timeout=30)
        rate = limits.get("rateLimits") or limits
        if rate:
            self._absorb_rate_limits(rate)
        return rate or None

    def quota_summary(self) -> Optional[dict]:
        """Normalized quota view: percent used and when it resets."""
        primary = (self.rate_limit or {}).get("primary") or {}
        used = primary.get("usedPercent")
        if used is None:
            return None
        return {
            "used_percent": used,
            "resets_at": primary.get("resetsAt"),
            "resets_human": _format_reset(primary.get("resetsAt")),
            "plan": (self.rate_limit or {}).get("planType"),
        }

    async def compact_context(self) -> dict:
        """Native Codex compaction — keeps the thread id."""
        await self._connect()
        if not self.session_id:
            raise RuntimeError("Codex thread is not initialized")
        if self._active_turn_id:
            raise RuntimeError("cannot compact during an active turn")
        async with asyncio.timeout(COMPACT_TIMEOUT_SECONDS):
            await self._request(
                "thread/compact/start",
                {"threadId": self.session_id},
                timeout=COMPACT_TIMEOUT_SECONDS,
            )
            # Measured event order (docs/tasks/16/spikes/compact_events.txt):
            # compact/start first INTERRUPTS any running turn, which emits its
            # own turn/completed{status:interrupted}, and only then opens a
            # SECOND turn carrying the contextCompaction item. Breaking on the
            # first turn/completed therefore returns before compaction has
            # begun — the bug this replaces.
            compaction_item: str | None = None
            while True:
                msg = await self._notifications.get()
                method = msg.get("method") or ""
                params = msg.get("params") or {}

                if method == "thread/tokenUsage/updated":
                    self._absorb_usage(params.get("tokenUsage") or {})
                    continue
                if method == "_process/exited":
                    raise RuntimeError("Codex app-server exited during compact")

                item = params.get("item") or {}
                if item.get("type") == "contextCompaction":
                    if method == "item/started":
                        compaction_item = str(item.get("id") or "")
                        continue
                    if method == "item/completed":
                        break
                # A turn/completed only ends the wait once the compaction item
                # has actually finished; the interrupt's own completion and the
                # compaction turn's opening are both passed over.
                if method == "turn/completed" and compaction_item is None:
                    if (params.get("turn") or {}).get("status") == "interrupted":
                        continue
                if method == "thread/compact/completed":
                    break

        # Context size is only knowable from the NEXT turn's inputTokens: the
        # usage notifications during compaction still carry the pre-compaction
        # totals, so this deliberately reports what is known rather than
        # inventing a post-compaction figure.
        return {
            "context_tokens": self._context_tokens,
            "max_tokens": self._context_window,
            "measured_after": False,
        }

    # ---------- lifecycle ----------

    async def interrupt(self) -> None:
        if not (self._active_turn_id and self.session_id and self.is_alive):
            return
        try:
            await asyncio.wait_for(
                self._request(
                    "turn/interrupt",
                    {"threadId": self.session_id, "turnId": self._active_turn_id},
                    timeout=INTERRUPT_TIMEOUT_SECONDS,
                ),
                timeout=INTERRUPT_TIMEOUT_SECONDS,
            )
            logger.info("Codex: interrupt sent")
        except Exception as exc:
            logger.warning(f"Codex interrupt failed: {exc}")

    def reconnect(self) -> None:
        """Drop the process, keep the thread id (mirrors ClaudeSession)."""
        self._connected = False
        proc = self._proc
        self._proc = None
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._active_turn_id = None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        logger.info("Codex: reconnecting (keeping thread id)")

    async def reset_async(self) -> None:
        """Forget the thread and wait for teardown before the next turn."""
        await self._teardown_process()
        self.session_id = None
        self._save_session()
        self._context_tokens = None
        self._last_ctx_usage = None
        self.usage_limit_active = False
        logger.info("Codex: session reset (thread cleared)")

    async def safe_disconnect(self) -> None:
        try:
            await self._teardown_process()
        except Exception:
            pass

    _safe_disconnect = safe_disconnect


def _toml_str(value: str) -> str:
    """TOML basic string for `-c key=value` overrides."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for admission control.

    Codex exposes no tokenizer over the wire. Cyrillic costs roughly a token
    per 2 characters, so this deliberately over-estimates rather than letting
    an oversized prompt through.
    """
    if not text:
        return 0
    return max(1, len(text) // 2)
