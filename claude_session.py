"""Claude session via ClaudeSDKClient — persistent connection with injection support."""

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    RateLimitEvent,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    McpSdkServerConfig,
    PermissionResultAllow,
)

logger = logging.getLogger(__name__)

SESSION_DIR = Path("./storage/sessions")

_USAGE_LIMIT_RE = re.compile(
    r"(hit\s+your\s+.*limit|session\s+limit|usage\s+limit|monthly\s+spend\s+limit)",
    re.IGNORECASE,
)
_RESET_RE = re.compile(r"resets?\s+([^\n.)]+?(?:\([^)]+\))?)\s*$", re.IGNORECASE)


def usage_limit_reset(err: str) -> str | None:
    """Return a localized reset suffix for a usage limit, or None otherwise."""
    if not _USAGE_LIMIT_RE.search(err):
        return None
    match = _RESET_RE.search(err.strip())
    return f" (сброс {match.group(1).strip()})" if match else ""


@dataclass
class _SessionReplacement:
    session_id: Optional[str]
    session_resumed: bool
    last_ctx_usage: Optional[dict]
    committed: bool = False


class ClaudeSession:
    def __init__(self, cwd: str, model: str = "claude-sonnet-4-6",
                 system_prompt: str = "",
                 mcp_servers: dict[str, McpSdkServerConfig] | None = None,
                 session_file: Optional[Path] = None,
                 on_connecting=None):
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt
        self.mcp_servers = mcp_servers or {}
        self._session_file = session_file or SESSION_DIR / "default"
        self._on_connecting = on_connecting
        self.session_id: Optional[str] = self._load_session()
        self.last_cost_usd: Optional[float] = None
        self.total_cost_usd: float = 0.0
        self.last_usage: Optional[dict[str, Any]] = None
        self.rate_limit: Optional[dict[str, Any]] = None
        self.usage_limit_active = False
        self.last_duration_ms: int = 0
        self.last_num_turns: int = 0
        self.last_stop_reason: Optional[str] = None
        self._client: Optional[ClaudeSDKClient] = None
        self._connected = False
        self.use_1m = True
        self._pending_disconnect = None
        self._last_ctx_usage: Optional[dict] = None
        self._expected_results = 0
        self._is_processing = False
        self._session_resumed = bool(self.session_id)
        self._query_lock = asyncio.Lock()
        self._session_replacement: Optional[_SessionReplacement] = None

    def _load_session(self) -> Optional[str]:
        if self._session_file.exists():
            sid = self._session_file.read_text().strip() or None
            if sid:
                logger.info(f"Loaded session from {self._session_file.name}: {sid[:8]}...")
                return sid
        return None

    def _save_session(self):
        if self._session_replacement and not self._session_replacement.committed:
            return
        self._write_session_id(self.session_id or "")

    def _write_session_id(self, session_id: str) -> None:
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=self._session_file.parent,
            prefix=f".{self._session_file.name}.",
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as temp_file:
                temp_file.write(session_id)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self._session_file)
            try:
                dir_fd = os.open(self._session_file.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                logger.warning(f"Session directory fsync failed after atomic replace: {exc}")
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def begin_session_replacement(self) -> _SessionReplacement:
        if self._session_replacement is not None:
            raise RuntimeError("session replacement already active")
        snapshot = _SessionReplacement(
            session_id=self.session_id,
            session_resumed=self._session_resumed,
            last_ctx_usage=self._last_ctx_usage,
        )
        self._session_replacement = snapshot
        self._pending_disconnect = self._client or self._pending_disconnect
        self._client = None
        self._connected = False
        self.session_id = None
        self._session_resumed = False
        self._last_ctx_usage = None
        return snapshot

    def commit_session_replacement(self, snapshot: _SessionReplacement) -> None:
        if self._session_replacement is not snapshot:
            raise RuntimeError("session replacement is not active")
        if not self.session_id:
            raise RuntimeError("candidate session has no session_id")
        self._write_session_id(self.session_id)
        snapshot.committed = True
        self._session_replacement = None

    async def rollback_session_replacement(self, snapshot: _SessionReplacement) -> None:
        if snapshot.committed:
            return
        if self._session_replacement is not snapshot:
            raise RuntimeError("session replacement is not active")

        clients = [client for client in (self._client, self._pending_disconnect) if client]
        self._client = None
        self._pending_disconnect = None
        self._connected = False
        self.session_id = snapshot.session_id
        self._session_resumed = snapshot.session_resumed
        self._last_ctx_usage = snapshot.last_ctx_usage
        self._session_replacement = None

        seen = set()
        for client in clients:
            if id(client) in seen:
                continue
            seen.add(id(client))
            await self._safe_disconnect(client)

    def _invalidate_session(self):
        self.session_id = None
        self._session_resumed = False
        self._save_session()

    @staticmethod
    async def _auto_approve_tool(tool_name, tool_input, _context=None):
        try:
            import json as _json
            _preview = _json.dumps(tool_input, ensure_ascii=False)[:200]
        except Exception:
            _preview = str(tool_input)[:200]
        logger.info(f"can_use_tool auto-allow: {tool_name} input={_preview}")
        return PermissionResultAllow(updated_input=tool_input)

    def _make_options(self) -> ClaudeAgentOptions:
        model = self.model
        if self.use_1m and "[1m]" not in model:
            model = f"{model}[1m]"
        options = ClaudeAgentOptions(
            model=model,
            cwd=self.cwd,
            max_turns=25,
            permission_mode="default",
            can_use_tool=self._auto_approve_tool,
            include_partial_messages=True,
        )
        if self.system_prompt:
            options.system_prompt = self.system_prompt
        if self.mcp_servers:
            options.mcp_servers = self.mcp_servers
        if self.session_id:
            options.resume = self.session_id
        return options

    async def _ensure_connected(self):
        if self._client and self._connected:
            return
        if self._pending_disconnect is not None:
            try:
                await self._pending_disconnect.disconnect()
            except Exception:
                pass
            self._pending_disconnect = None
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._on_connecting is not None:
            self._on_connecting()
        options = self._make_options()
        self._client = ClaudeSDKClient(options=options)
        if self.session_id:
            logger.info(f"Connecting with resume {self.session_id[:8]}...")
        else:
            logger.info("Connecting new session...")
        try:
            await self._client.connect()
        except Exception as e:
            if self.session_id and ("No conversation found" in str(e) or "exit code 1" in str(e)):
                logger.warning("Session %s expired, invalidating", self.session_id[:8])
                self._invalidate_session()
                options = self._make_options()
                self._client = ClaudeSDKClient(options=options)
                await self._client.connect()
            else:
                raise
        self._connected = True

    async def send_message(self, text: str) -> AsyncGenerator[dict, None]:
        logger.info(f"Prompt: {text[:150]}...")
        pending_limit: Optional[str] = None
        limit_seen = False
        limit_content = ""
        batch_had_error = False
        generic_errors: list[str] = []

        try:
            logger.info("send_message: ensuring connected...")
            await self._ensure_connected()
            logger.info("send_message: connected, sending query...")
            async with self._query_lock:
                await self._client.query(text)
                self._expected_results = 1
                self._is_processing = True
            logger.info("send_message: query sent, receiving messages...")

            async for msg in self._client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    assistant_error = getattr(msg, "error", None)
                    if assistant_error in {"rate_limit", "billing_error"}:
                        raw = "\n".join(
                            block.text for block in msg.content
                            if isinstance(block, TextBlock) and block.text
                        )
                        pending_limit = raw or assistant_error
                        limit_content = limit_content or pending_limit
                        limit_seen = True
                        continue
                    if limit_seen:
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {"type": "tool", "name": block.name, "input": block.input}
                        elif isinstance(block, ToolResultBlock):
                            content = block.content if isinstance(block.content, str) else str(block.content)
                            yield {"type": "result", "content": content[:200], "error": block.is_error}
                elif isinstance(msg, ResultMessage):
                    if getattr(msg, "session_id", None):
                        self.session_id = msg.session_id
                        self._save_session()
                        logger.info(f"Session ID saved: {self.session_id[:8]}...")
                    if getattr(msg, "total_cost_usd", None) is not None:
                        self.last_cost_usd = msg.total_cost_usd
                        self.total_cost_usd += msg.total_cost_usd
                    if getattr(msg, "usage", None):
                        self.last_usage = msg.usage
                    self.last_duration_ms = getattr(msg, "duration_ms", 0) or 0
                    self.last_num_turns = getattr(msg, "num_turns", 0) or 0
                    self.last_stop_reason = getattr(msg, "stop_reason", None)
                    dur_s = self.last_duration_ms / 1000
                    logger.info(
                        f"Result: {dur_s:.1f}s, {self.last_num_turns} turns, "
                        f"stop={self.last_stop_reason}, cost=${self.last_cost_usd or 0:.4f}"
                    )

                    raw_result = str(msg.result or "")
                    result_is_limit = (
                        pending_limit is not None
                        or getattr(msg, "api_error_status", None) == 429
                        or getattr(msg, "terminal_reason", None) == "blocking_limit"
                        or usage_limit_reset(raw_result) is not None
                    )
                    if result_is_limit:
                        limit_seen = True
                        limit_content = limit_content or pending_limit or raw_result or "usage limit"
                        self.usage_limit_active = True
                    elif msg.is_error:
                        batch_had_error = True
                        if raw_result:
                            generic_errors.append(raw_result)
                    pending_limit = None

                    async with self._query_lock:
                        self._expected_results = max(0, self._expected_results - 1)
                        terminal = self._expected_results == 0
                        if terminal:
                            self._is_processing = False

                    if terminal:
                        if limit_seen:
                            yield {
                                "type": "error",
                                "kind": "usage_limit",
                                "content": limit_content or "usage limit",
                            }
                        else:
                            if not batch_had_error:
                                self.usage_limit_active = False
                            for error in generic_errors:
                                yield {"type": "error", "content": error}
                        break
                    if not limit_seen:
                        yield {"type": "turn_done"}
                elif isinstance(msg, StreamEvent):
                    if limit_seen:
                        continue
                    evt = msg.event
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield {"type": "text_delta", "content": delta.get("text", "")}
                elif isinstance(msg, SystemMessage):
                    logger.info(f"System: {msg.subtype}")
                elif isinstance(msg, RateLimitEvent):
                    rl = msg.rate_limit_info
                    self.rate_limit = {
                        "status": rl.status,
                        "type": rl.rate_limit_type,
                        "utilization": rl.utilization,
                    }
                    if rl.status == "rejected":
                        pending_limit = pending_limit or "usage limit"
                        limit_content = limit_content or pending_limit
                        limit_seen = True
                    logger.info(f"Rate limit: {rl.status} ({rl.rate_limit_type}) util={rl.utilization}")
        except Exception as e:
            err = str(e)
            if usage_limit_reset(err) is not None:
                self.usage_limit_active = True
                self._connected = False
                self._client = None
                self._expected_results = 0
                yield {"type": "error", "kind": "usage_limit", "content": err}
                return
            if self.session_id and ("No conversation found" in err or "exit code 1" in err):
                logger.warning(
                    "Session %s failed (%s), invalidating and retrying",
                    self.session_id[:8], type(e).__name__,
                )
                self._invalidate_session()
                self._connected = False
                self._client = None
                async for chunk in self.send_message(text):
                    yield chunk
                return
            logger.error(f"SDK error: {e}", exc_info=True)
            self._connected = False
            self._client = None
            yield {"type": "error", "content": err}
        finally:
            if self._is_processing:
                async with self._query_lock:
                    self._is_processing = False

    async def inject(self, text: str) -> bool:
        if not (self._client and self._connected and self._is_processing):
            return False
        try:
            async with self._query_lock:
                if not (self._client and self._connected and self._is_processing):
                    return False
                await self._client.query(text)
                self._expected_results += 1
                expected = self._expected_results
            logger.info(f"Injected (expect {expected} results): {text[:80]}...")
            return True
        except Exception as e:
            logger.error(f"Inject error: {e}")
            return False

    async def interrupt(self):
        if self._client and self._connected:
            try:
                await self._client.interrupt()
                logger.info("Interrupt sent")
            except Exception as e:
                logger.error(f"Interrupt error: {e}")

    async def get_context_usage(self) -> Optional[dict]:
        if self._client and self._connected:
            try:
                result = await self._client.get_context_usage()
                if result and result.get("percentage", 0) > 0:
                    self._last_ctx_usage = result
                    return result
                elif self._last_ctx_usage and result and result.get("percentage", 0) == 0:
                    logger.warning(f"get_context_usage returned 0%, using cached {self._last_ctx_usage.get('percentage', 0):.0f}%")
                    return self._last_ctx_usage
                return result
            except Exception as e:
                logger.error(f"get_context_usage error: {e}")
        if self._last_ctx_usage:
            logger.warning("get_context_usage: client unavailable, using cached value")
            return self._last_ctx_usage
        return None

    def reconnect(self):
        self._connected = False
        old_client = self._client
        self._client = None
        self._pending_disconnect = old_client
        logger.info("Session reconnecting (keeping session_id)")

    async def reset_async(self):
        """Reset session and WAIT for disconnect to complete before returning.
        Use this when immediately calling send_message() on a new session."""
        self._invalidate_session()
        self._last_ctx_usage = None
        self._connected = False
        old_client = self._client
        self._client = None
        if old_client:
            try:
                await old_client.disconnect()
            except Exception as e:
                logger.debug(f"reset_async disconnect error: {e}")
        logger.info("Session reset (cleared session_id, disconnect awaited)")

    def reset(self):
        self._invalidate_session()
        self._last_ctx_usage = None
        self.reconnect()
        logger.info("Session reset (cleared session_id)")

    async def _safe_disconnect(self, client=None):
        client = client or self._client
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass
