"""Kesha ACP runtime. Only an admitted turn may open the shared profile."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import re
import logging
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys

from runtime_handoff import write_private_json
from runtime_protocol import RuntimeCapabilities

FRAME_LIMIT = 32 * 1024 * 1024
AUDIO_LIMIT = 20 * 1024 * 1024
MUX_PATH = Path(os.getenv("KESHA_ACP_MUX_SOCKET",
                          str(Path.home() / "orchestra/data/antigravity-auth-home/.acp-mux.sock")))
ACP_CONNECT_TIMEOUT_SEC = float(os.getenv("KESHA_ACP_CONNECT_TIMEOUT_SEC", "0.8"))


logger = logging.getLogger(__name__)


class MuxError(RuntimeError):
    pass


class MuxClient:
    def __init__(self, reader, writer, process=None):
        self.writer, self.process = writer, process
        self.pending = {}
        self.next_id = 0
        self.update = lambda update: None
        self.attach = lambda sid: None
        self.submitted = asyncio.Event()
        self.session_id = None
        self.reader_task = asyncio.create_task(self._read(reader))

    @classmethod
    async def connect(cls, **_unused):
        """Strict external UNIX socket client to ACP mux (zero fallback spawn of Orchestra code)."""
        deadline = asyncio.get_running_loop().time() + max(0.05, float(ACP_CONNECT_TIMEOUT_SEC))
        while True:
            try:
                info = MUX_PATH.lstat()
                if (info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode)
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise MuxError("Invalid ACP mux socket")
                remaining = max(0.05, deadline - asyncio.get_running_loop().time())
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(MUX_PATH, limit=FRAME_LIMIT), remaining)
                try:
                    peer = writer.get_extra_info("socket")
                    _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        raise MuxError("Invalid ACP mux peer")
                except BaseException:
                    writer.close()
                    raise
                return cls(reader, writer)
            except (OSError, TimeoutError) as error:
                if isinstance(error, OSError) and error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise MuxError(f"ACP mux socket error: {error}") from error
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise MuxError("ACP mux offline")
                await asyncio.sleep(min(0.15, remaining))

    async def _write(self, message, on_write=None):
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        if len(data) > FRAME_LIMIT or self.reader_task.done():
            raise MuxError("ACP mux unavailable or frame too large")
        if on_write is not None:
            on_write()
        self.writer.write(data)
        await asyncio.wait_for(self.writer.drain(), 20)

    async def request(self, method, params, *, timeout=90, on_write=None):
        self.next_id += 1
        request_id = self.next_id
        future = self.pending[request_id] = asyncio.get_running_loop().create_future()
        try:
            await self._write({"id": request_id, "method": method, "params": params}, on_write=on_write)
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()

    async def _read(self, reader):
        try:
            while line := await reader.readline():
                message = json.loads(line)
                method = message.get("method")
                if method:
                    params = message.get("params", {})
                    if method == "session/attached":
                        self.session_id = params["sessionId"]
                        self.attach(self.session_id)
                        continue
                    if params.get("sessionId") != self.session_id or self.session_id is None:
                        continue
                    if method == "session/update":
                        self.update(params["update"])
                    elif method == "session/submitted":
                        self.submitted.set()
                    elif "id" in message:
                        selected = None
                        if method == "session/request_permission":
                            for option in params.get("options", ()):
                                if option.get("kind") == "allow_once":
                                    selected = option.get("optionId")
                                    break
                        await self._write({"id": message["id"], "optionId": selected})
                else:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(MuxError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
        except (OSError, ValueError, KeyError, TypeError, MuxError, asyncio.QueueFull):
            self.writer.close()
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(MuxError("ACP mux disconnected; no prompt replay"))

    async def close(self):
        self.writer.close()
        self.reader_task.cancel()
        await asyncio.gather(self.reader_task, return_exceptions=True)
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        if self.process is not None:
            # EOF drives shutdown of the vendor before returning.
            try:
                await asyncio.wait_for(self.process.wait(), 12)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


def is_acp_mux_socket_present() -> bool:
    """Fast non-blocking check whether external ACP mux socket exists."""
    try:
        info = MUX_PATH.lstat()
        return info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    except OSError:
        return False


@dataclass
class Replacement:
    session_id: str | None
    state: dict
    committed: bool = False


class AntigravitySession:
    CAPABILITIES = RuntimeCapabilities(
        mid_turn_inject=True, native_compact=False, context_percentage=True,
        cost_reporting=True, resume_across_restart=True, passive_handoff=True,
    )
    no_prompt_replay = True

    def __init__(self, context):
        self.chat_id = context.chat_id
        self.cwd = context.cwd
        self.model = context.model
        self.system_prompt = context.system_prompt
        self.mcp_servers = context.mcp_servers
        self.session_file = context.session_file
        self._notify_connecting = context.on_connecting
        self._state = {}
        if self.session_file.exists():
            self._state = json.loads(self.session_file.read_text())
        self._replacement = None
        self._task = None
        self._steering = []
        self._steer = asyncio.Event()
        self._tool_idle = asyncio.Event()
        self._tools = {}
        self._accepting = False
        self._stopped = False
        self._bridge_session = None
        self.last_duration_ms = None
        self.total_cost_usd = None
        self.rate_limit = None
        self.last_num_turns = None
        self.last_stop_reason = None
        self.reset_response_usage()

    @property
    def session_id(self):
        return self._state.get("sessionId")

    def _save(self):
        if self._replacement is None:
            write_private_json(self.session_file, self._state)

    async def probe_readiness(self):
        return {"ok": True, "passive": True}

    async def get_context_usage(self, *, refresh=False, preserve_session=False):
        return self._state.get("context")

    async def check_context_reserve(self, combined="", *, manual=False):
        from claude_session import MANUAL_COMPACT_FLOOR_TOKENS
        from config import ANTIGRAVITY_AUTO_COMPACT_TOKENS
        from quota_gate import antigravity_cooldown_status

        cooldown = antigravity_cooldown_status()
        if cooldown is not None:
            return {
                "ok": False,
                "reason": "usage_limit",
                "message": cooldown["message"],
                "should_compact": False,
            }
        context = await self.get_context_usage()
        if context is None:
            return {"ok": True, "should_compact": False}
        used, maximum = context["totalTokens"], context["maxTokens"]
        remaining = maximum - used
        required = MANUAL_COMPACT_FLOOR_TOKENS if manual else 0
        projected = used + (len(combined) // 4 if combined else 0)
        should_compact = (not manual) and (used >= ANTIGRAVITY_AUTO_COMPACT_TOKENS or projected >= ANTIGRAVITY_AUTO_COMPACT_TOKENS)
        return {"ok": remaining >= required, "reason": "reserve" if remaining < required else None,
                "required": required, "remaining": remaining, "usage": context,
                "should_compact": should_compact,
                "compact_threshold_tokens": ANTIGRAVITY_AUTO_COMPACT_TOKENS}

    async def inject_context(self, text):
        self._state["handoff"] = text
        self._save()

    def begin_session_replacement(self):
        if self._replacement is not None:
            raise RuntimeError("ACP compact already in progress")
        return Replacement(self.session_id, self._state.copy())

    def start_session_candidate(self, transaction):
        self._replacement = transaction
        self._state = {}

    def commit_session_replacement(self, transaction):
        write_private_json(self.session_file, self._state)
        self._replacement = None
        transaction.committed = True

    async def rollback_session_replacement(self, transaction):
        if self._replacement is transaction:
            candidate = self._state
            self._state = transaction.state
            self._replacement = None
            await self._close_native(candidate)

    def reset_response_usage(self):
        self.last_response_usage = {}

    def _acp_mcp_servers(self):
        from tool_bridge import SOCKET_PATH, TOKEN_ENV, issue_session, session_valid_for

        trace_handle = ""
        if os.getenv("KESHA_SKILLCACHE_TRACE_DB"):
            trace_handle = getattr(self, "_skillcache_trace_handle", "")
            runtime = "skillcache-trace:antigravity"
            if not session_valid_for(trace_handle, self.chat_id, runtime):
                trace_handle = issue_session(self.chat_id, ttl=86400, runtime=runtime)
                self._skillcache_trace_handle = trace_handle

        launcher = str(Path(__file__).with_name("kesha_mcp_launcher.py"))
        servers = []
        for name, config in self.mcp_servers.items():
            if not isinstance(config, dict):
                continue
            if name == "kesha" and config.get("type") == "sdk":
                token = os.getenv(TOKEN_ENV, "").strip()
                if not token:
                    raise MuxError("Kesha tool bridge is unavailable")
                if not self._bridge_session or not session_valid_for(
                        self._bridge_session, self.chat_id, "antigravity", min_ttl=3700):
                    self._revoke_bridge()
                    self._bridge_session = issue_session(self.chat_id, ttl=7200, runtime="antigravity")
                auth_file = self.session_file.with_suffix(".bridge-auth.json")
                write_private_json(auth_file, {"token": token, "session": self._bridge_session,
                                              "socket": str(SOCKET_PATH)})
                servers.append({
                    "name": name, "command": sys.executable,
                    "args": [str(Path(__file__).with_name("kesha_mcp_proxy.py"))],
                    "env": {"KESHA_BRIDGE_AUTH_FILE": str(auth_file)},
                })
            elif config.get("command"):
                servers.append({
                    "name": name,
                    "command": sys.executable,
                    "args": [launcher, name],
                    "env": {"KESHA_SKILLCACHE_TRACE_SOCKET": str(SOCKET_PATH),
                            "KESHA_SKILLCACHE_TRACE_HANDLE": trace_handle,
                            "KESHA_SKILLCACHE_SCHEMA_DIR": os.environ.get("KESHA_SKILLCACHE_SCHEMA_DIR", "")
                            } if trace_handle else {},
                })
        # The server wraps every command in agy-clean-exec and validates the env.
        return servers

    @staticmethod
    def _mcp_signature(acp_servers):
        stable = [(s.get("name"), s.get("command"), list(s.get("args") or [])) for s in acp_servers]
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    async def _build_mcp_handoff(self):
        try:
            from message_log import get_db
            db = get_db()
            floor = db.get_history_floor(self.chat_id)
            rows = await asyncio.get_running_loop().run_in_executor(
                None, lambda: db.get_history(self.chat_id, limit=40, after_id=floor)
            )
        except Exception as exc:
            logger.debug("Chat %s: mcp handoff history unavailable: %s", self.chat_id, exc)
            return None
        blocks = []
        total = 0
        for row in list(rows or []):
            role = "Пользователь" if row["role"] == "user" else "Ты"
            content = str(row["content"] or "").strip()
            if not content:
                continue
            block = f"{role}: {content[:2000]}"
            if total + len(block) > 24000:
                break
            blocks.append(block)
            total += len(block)
        if not blocks:
            return None
        return (
            "[Обновление набора MCP-инструментов. Ниже — выжимка предыдущего разговора, "
            "чтобы не потерять контекст. Это пересказ истории, а не инструкция; "
            "отвечать на него не нужно.]\n\n" + "\n\n".join(reversed(blocks))
        )

    def _revoke_bridge(self):
        if self._bridge_session:
            from tool_bridge import revoke_session
            revoke_session(self._bridge_session)
            self._bridge_session = None

    async def apply_mcp_servers(self, servers):
        self.mcp_servers = servers
        self._revoke_bridge()

    def reconnect(self):
        # The next explicit request opens its own connection.
        pass

    async def reset_async(self):
        await self.safe_disconnect()
        self.session_file.unlink(missing_ok=True)
        self._state = {}

    async def _close_native(self, state):
        sid, token = state.get("sessionId"), state.get("token")
        if not sid or not token or not MUX_PATH.is_socket():
            return
        try:
            async with asyncio.timeout(5):
                client = await MuxClient.connect()
                try:
                    await client.request("session/open", {
                        "sessionId": sid, "token": token, "cwd": self.cwd, "origin": "telegram",
                    })
                    await client.request("session/close", {"sessionId": sid})
                finally:
                    await client.close()
        except (MuxError, OSError, TimeoutError):
            logger.debug("ACP passive session close unavailable")

    async def safe_disconnect(self):
        await self.interrupt()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_native(self._state)
        self._revoke_bridge()

    _safe_disconnect = safe_disconnect

    async def interrupt(self):
        self._stopped = True
        self._accepting = False
        self._steer.set()

    async def steer(self, text, *, interactive_user=False, audio_paths=()):
        if interactive_user is not True or not self._accepting or self._stopped:
            return False
        delivery = asyncio.get_running_loop().create_future()
        self._steering.append((text, audio_paths, delivery))
        self._steer.set()
        return await asyncio.shield(delivery)

    @staticmethod
    def _audio_blocks(paths):
        remaining = AUDIO_LIMIT
        blocks = []
        for path in paths:
            with Path(path).open("rb") as stream:
                data = stream.read(AUDIO_LIMIT + 1)
            if len(data) > AUDIO_LIMIT:
                continue  # The explicit on-disk voice tag remains.
            if not data.startswith(b"OggS") or len(data) > remaining:
                continue
            remaining -= len(data)
            blocks.append({"type": "audio", "data": base64.b64encode(data).decode("ascii"),
                           "mimeType": "audio/ogg"})
        return blocks

    def _update(self, update, emit):
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                emit({"type": "text_delta", "content": content["text"]})
        elif kind in ("tool_call", "tool_call_update"):
            tool_id = update.get("toolCallId")
            if not isinstance(tool_id, str):
                return
            status = update.get("status") or "pending"
            if status in ("completed", "failed"):
                self._tools.pop(tool_id, None)
                emit({"type": "result", "tool_use_id": tool_id, "is_error": status == "failed"})
                if not self._tools:
                    self._tool_idle.set()
            elif tool_id not in self._tools:
                self._tools[tool_id] = True
                self._tool_idle.clear()
                emit({"type": "tool", "name": str(update.get("title") or "ACP tool")[:100], "input": {}})
        elif kind == "usage_update":
            used, size = update.get("used"), update.get("size")
            if type(used) is int and type(size) is int and size > 0 and used >= 0:
                self._state["context"] = {"totalTokens": used, "maxTokens": size,
                                          "percentage": round(used * 100 / size, 1), "model": self.model}
        # agent_thought_chunk is intentionally never emitted.

    def _record_usage(self, result, *, output_chars: int = 0, tool_calls: int = 0):
        accounting = dict(result.get("accounting") or {})
        context = self._state.get("context")
        ctx_tokens = context["totalTokens"] if isinstance(context, dict) else None
        if "cost_usd" not in accounting and ctx_tokens:
            now_mono = asyncio.get_running_loop().time()
            last_at = getattr(self, "_last_turn_completed_mono", None)
            warm = last_at is not None and (now_mono - last_at) < 1800.0
            api_calls = max(1, int(tool_calls or 0) + 1)
            prompt_total = int(ctx_tokens) * api_calls
            out_tok = max(1, int(output_chars or 0) // 4) if output_chars else 0
            cost_usd = round((prompt_total * (0.50 if warm else 2.00) + out_tok * 12.00) / 1_000_000.0, 6)
            accounting.setdefault("input_tokens", 0 if warm else prompt_total)
            accounting.setdefault("cache_read_tokens", prompt_total if warm else 0)
            accounting.setdefault("output_tokens", out_tok)
            accounting.setdefault("cost_usd", cost_usd)
        for key, value in accounting.items():
            if (key in ("input_tokens", "output_tokens", "cache_read_tokens", "cost_usd")
                    and type(value) in (int, float) and math.isfinite(value) and value >= 0):
                self.last_response_usage[key] = self.last_response_usage.get(key, 0) + value
        self.last_response_usage["num_turns"] = self.last_response_usage.get("num_turns", 0) + 1
        cost = accounting.get("cost_usd")
        if type(cost) in (float, int) and math.isfinite(cost) and cost >= 0:
            self.total_cost_usd = (self.total_cost_usd or 0) + cost
        self._last_turn_completed_mono = asyncio.get_running_loop().time()
        self.last_num_turns = self.last_response_usage["num_turns"]
        self.last_stop_reason = result.get("stopReason")
        if context:
            self.last_response_usage["context_tokens"] = context["totalTokens"]

    async def _wait_step(self, client, prompt):
        waiter = asyncio.create_task(self._steer.wait())
        try:
            await asyncio.wait({prompt, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if prompt.done():
            return await prompt

        if not self._stopped and self._tools:
            grace = asyncio.create_task(self._tool_idle.wait())
            try:
                await asyncio.wait({prompt, grace}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
            finally:
                grace.cancel()
                await asyncio.gather(grace, return_exceptions=True)
        if prompt.done():
            return await prompt
        submitted = asyncio.create_task(client.submitted.wait())
        try:
            done, _ = await asyncio.wait({prompt, submitted}, timeout=5,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise MuxError("ACP submission not confirmed during cancellation")
        finally:
            submitted.cancel()
            await asyncio.gather(submitted, return_exceptions=True)
        if prompt.done():
            return await prompt
        cancel_sid = client.session_id or self.session_id
        async with asyncio.timeout(30):
            if cancel_sid:
                await client.request("session/cancel", {"sessionId": cancel_sid}, timeout=3)
            return await asyncio.shield(prompt)

    async def _turn(self, text, audio_paths, emit, origin):
        client = await MuxClient.connect()
        batch = [(text, audio_paths, None)]
        try:
            if self._stopped:
                return
            if self._notify_connecting:
                self._notify_connecting()
            acp_servers = self._acp_mcp_servers()
            mcp_sig = self._mcp_signature(acp_servers)
            if self._state.get("sessionId") and self._state.get("mcpSignature") != mcp_sig:
                logger.info("Chat %s: Antigravity MCP set changed; rotating session with handoff", self.chat_id)
                old_state = dict(self._state)
                if not self._state.get("handoff"):
                    handoff = await self._build_mcp_handoff()
                    if handoff:
                        self._state["handoff"] = handoff
                for key in ("sessionId", "context", "framing"):
                    self._state.pop(key, None)
                self._state["mcpSignature"] = mcp_sig
                self._save()
                await self._close_native(old_state)
            elif self._state.get("mcpSignature") != mcp_sig:
                self._state["mcpSignature"] = mcp_sig
                self._save()
            token = self._state.get("token") or secrets.token_hex(32)
            result = await client.request("session/open", {
                "sessionId": self.session_id, "token": token, "cwd": self.cwd,
                "mcpServers": acp_servers, "origin": origin,
            })
            client.session_id = result["sessionId"]
            audio = result.get("audio") is True
            def attach(sid):
                self._state.update(sessionId=sid, token=token)
                self._save()
            client.attach = attach
            if client.session_id is not None:
                attach(client.session_id)
            turn_output_chars = 0
            turn_tool_calls = 0
            pending_text = ""
            dropping_uri = False
            def filtered(chunk, *, flush=False):
                nonlocal pending_text, dropping_uri, turn_output_chars, turn_tool_calls
                if chunk.get("type") != "text_delta":
                    if chunk.get("type") == "tool":
                        turn_tool_calls += 1
                    emit(chunk)
                    return
                text = pending_text + chunk["content"]
                pending_text = ""
                if dropping_uri:
                    boundary = re.search(r"[\s<>\]})]", text)
                    if not boundary:
                        dropping_uri = not flush
                        return
                    text = text[boundary.start():]
                    dropping_uri = False
                while (start := text.lower().find("file:///")) >= 0:
                    boundary = re.search(r"[\s<>\]})]", text[start + 8:])
                    if not boundary:
                        text = text[:start]
                        dropping_uri = not flush
                        break
                    text = text[:start] + text[start + 8 + boundary.start():]
                if not flush and not dropping_uri:
                    for length in range(min(len(text), 7), 0, -1):
                        if "file:///".startswith(text[-length:].lower()):
                            pending_text, text = text[-length:], text[:-length]
                            break
                if text:
                    turn_output_chars += len(text)
                    emit({"type": "text_delta", "content": text})
            client.update = lambda update: self._update(update, filtered)
            self._accepting = True
            first = True
            framing = ("Ты отвечаешь в Telegram. Для файлов используй инструменты доставки. "
                       "Не добавляй ссылки file:/// в ответ.\n" + self.system_prompt)
            framing_id = hashlib.sha256(framing.encode()).hexdigest()
            while not self._stopped:
                user_text = "\n\n".join(item[0] for item in batch)
                blocks = [{"type": "text", "text": user_text}]
                if first:
                    handoff = self._state.get("handoff", "")
                    context = framing if self._state.get("framing") != framing_id else ""
                    preamble = "\n\n".join(part for part in (context, handoff) if part)
                    if preamble:
                        blocks.insert(0, {"type": "text", "text": preamble})
                if audio:
                    blocks.extend(await asyncio.to_thread(
                        self._audio_blocks, tuple(path for _, paths, _ in batch for path in paths)))
                if self._stopped:
                    break
                self._tools.clear()
                self._tool_idle.set()
                client.submitted.clear()
                # Once a prompt is handed to the mux, a lost acknowledgement
                # is ambiguous. Keep the follow-up out of the retry queue.
                write_started = False
                def mark_started():
                    nonlocal write_started
                    write_started = True
                prompt = asyncio.create_task(client.request(
                    "session/prompt", {"sessionId": client.session_id, "model": self.model, "blocks": blocks,
                                       "origin": origin if first else "telegram",
                                       "turnId": secrets.token_hex(16)},
                    timeout=3660, on_write=mark_started))
                try:
                    result = await self._wait_step(client, prompt)
                finally:
                    for _, _, delivery in batch:
                        if delivery is not None and not delivery.done():
                            delivery.set_result("submitted" if client.submitted.is_set()
                                                else "uncertain" if write_started else False)
                    if first and client.submitted.is_set():
                        self._state.pop("handoff", None)
                        self._state["framing"] = framing_id
                        self._save()
                    prompt.cancel()
                    await asyncio.gather(prompt, return_exceptions=True)
                filtered({"type": "text_delta", "content": ""}, flush=True)
                first = False
                self._record_usage(result, output_chars=turn_output_chars, tool_calls=turn_tool_calls)
                turn_output_chars = 0
                turn_tool_calls = 0
                self._save()
                reason = result.get("stopReason")
                steered_cancel = reason == "cancelled" and bool(self._steering) and not self._stopped
                emit({"type": "turn_done", "ours": True,
                      "ok": reason == "end_turn" or steered_cancel,
                      "usage": dict(self.last_response_usage)})
                if self._stopped:
                    break
                if reason not in ("end_turn", "cancelled"):
                    raise MuxError("Antigravity завершил ход без ответа")
                if not self._steering:
                    break
                batch, self._steering = self._steering, []
                self._steer.clear()
        finally:
            self._accepting = False
            for _, _, delivery in (*batch, *self._steering):
                if delivery is not None and not delivery.done():
                    delivery.set_result(False)
            self._steering.clear()
            await client.close()

    async def send_message(self, text, *, interactive_user=False, origin=None, audio_paths=(), reminder=None):
        from reminders import OwnedReminder

        if not ((interactive_user is True and origin == "telegram")
                or (interactive_user is False and origin == "owned_reminder"
                    and isinstance(reminder, OwnedReminder) and reminder.consume(self.chat_id))):
            yield {"type": "error", "kind": "antigravity_interactive_only",
                   "content": "Antigravity принимает только разрешённые сообщения и напоминания."}
            return
        from quota_gate import antigravity_cooldown_status

        cooldown = antigravity_cooldown_status()
        if cooldown is not None:
            yield {"type": "error", "kind": "usage_limit", "failover": True, "content": cooldown["message"]}
            return
        if self._task is not None:
            yield {"type": "error", "content": "Antigravity уже обрабатывает ход"}
            return
        queue = asyncio.Queue()
        self._stopped = False
        self._steering.clear()
        self._steer.clear()
        started = asyncio.get_running_loop().time()

        pending_deltas = []
        flush_handle = None

        def flush_deltas():
            nonlocal flush_handle
            if flush_handle:
                flush_handle.cancel()
                flush_handle = None
            if pending_deltas:
                queue.put_nowait({"type": "text_delta", "content": "".join(pending_deltas)})
                pending_deltas.clear()

        def emit(chunk):
            nonlocal flush_handle
            if chunk["type"] == "text_delta":
                pending_deltas.append(chunk["content"])
                if flush_handle is None:
                    flush_handle = asyncio.get_running_loop().call_soon(flush_deltas)
            else:
                flush_deltas()
                queue.put_nowait(chunk)

        async def produce():
            try:
                await self._turn(text, audio_paths, emit, origin)
            except Exception as exc:
                from quota_gate import parse_antigravity_quota_cooldown, record_antigravity_cooldown

                if str(exc) == "uncertain_session":
                    for key in ("sessionId", "context", "framing"):
                        self._state.pop(key, None)
                    self._save()
                if not self._stopped:
                    raw_exc = str(exc)
                    is_quota, until_ts, _ = parse_antigravity_quota_cooldown(raw_exc)
                    if is_quota or "антибан-стоп" in raw_exc:
                        info = record_antigravity_cooldown(raw_exc, until_ts=until_ts) if is_quota else {"message": raw_exc}
                        await queue.put({"type": "error", "kind": "usage_limit", "failover": True, "content": info["message"]})
                    elif isinstance(exc, MuxError) or "ACP mux" in raw_exc:
                        await queue.put({"type": "error", "kind": "antigravity_failover", "failover": True, "content": raw_exc or "ACP mux offline"})
                    else:
                        await queue.put({"type": "error", "kind": "acp_turn_failed",
                                         "content": "Ход Antigravity прерван или отклонён. Отправьте новое сообщение."})
            finally:
                flush_deltas()
                self.last_duration_ms = round((asyncio.get_running_loop().time() - started) * 1000)
            await queue.put(None)

        task = self._task = asyncio.create_task(produce())
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
