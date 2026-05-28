"""Claude session via ClaudeSDKClient — persistent connection with injection support."""


import logging
import time
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
        self.session_id_changed_at: int = 0
        self.last_cost_usd: Optional[float] = None
        self.total_cost_usd: float = 0.0
        self.last_usage: Optional[dict[str, Any]] = None
        self.rate_limit: Optional[dict[str, Any]] = None
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

    def _load_session(self) -> Optional[str]:
        redis_sid, redis_ts = self._load_session_from_redis()
        file_sid = None
        if self._session_file.exists():
            file_sid = self._session_file.read_text().strip() or None
        if redis_sid and redis_sid != file_sid:
            logger.info(f"Session from Redis: {redis_sid[:8]}... ts={redis_ts} (file had {file_sid[:8] + '...' if file_sid else 'none'})")
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_file.write_text(redis_sid)
            self.session_id_changed_at = redis_ts
            return redis_sid
        if file_sid:
            logger.info(f"Loaded session from {self._session_file.name}: {file_sid[:8]}...")
            return file_sid
        return None

    def _load_session_from_redis(self) -> tuple[str | None, int]:
        try:
            from config import KESHA_REDIS_URL
            if not KESHA_REDIS_URL:
                return None, 0
            import redis as sync_redis
            chat_id = self._session_file.stem
            r = sync_redis.from_url(KESHA_REDIS_URL, decode_responses=True, socket_timeout=3)
            raw = r.get(f"kesha:sessions:{chat_id}")
            r.close()
            if not raw:
                return None, 0
            if ":" in raw:
                sid, ts_str = raw.rsplit(":", 1)
                return sid, int(ts_str)
            return raw, 0
        except Exception:
            return None, 0

    def _save_session(self):
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(self.session_id or "")

    def _save_session_to_redis(self):
        try:
            from config import KESHA_REDIS_URL
            if not KESHA_REDIS_URL:
                return
            import redis as sync_redis
            chat_id = self._session_file.stem
            r = sync_redis.from_url(KESHA_REDIS_URL, decode_responses=True, socket_timeout=3)
            if self.session_id:
                ts = self.session_id_changed_at or int(time.time())
                r.set(f"kesha:sessions:{chat_id}", f"{self.session_id}:{ts}")
            else:
                r.delete(f"kesha:sessions:{chat_id}")
            r.close()
        except Exception as e:
            logger.warning(f"Redis session save failed for {self._session_file.stem}: {e}")

    def _invalidate_session(self):
        self.session_id = None
        self._session_resumed = False
        self._save_session()
        self._save_session_to_redis()

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
        redis_sid, redis_ts = self._load_session_from_redis()
        if redis_sid and redis_sid != self.session_id and redis_ts >= self.session_id_changed_at:
            logger.info(f"Session refreshed from Redis: {redis_sid[:8]}... ts={redis_ts} (was {self.session_id[:8] + '...' if self.session_id else 'none'} ts={self.session_id_changed_at})")
            self.session_id = redis_sid
            self.session_id_changed_at = redis_ts
            self._save_session()
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
                logger.warning("Session %s expired, invalidating (file+Redis)", self.session_id[:8])
                self._invalidate_session()
                options = self._make_options()
                self._client = ClaudeSDKClient(options=options)
                await self._client.connect()
            else:
                raise
        self._connected = True

    async def send_message(self, text: str) -> AsyncGenerator[dict, None]:
        logger.info(f"Prompt: {text[:150]}...")

        try:
            logger.info("send_message: ensuring connected...")
            await self._ensure_connected()
            logger.info("send_message: connected, sending query...")
            await self._client.query(text)
            logger.info("send_message: query sent, receiving messages...")
            self._expected_results = 1
            self._is_processing = True

            async for msg in self._client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {"type": "tool", "name": block.name, "input": block.input}
                        elif isinstance(block, ToolResultBlock):
                            content = block.content if isinstance(block.content, str) else str(block.content)
                            yield {"type": "result", "content": content[:200], "error": block.is_error}
                elif isinstance(msg, ResultMessage):
                    if hasattr(msg, 'session_id') and msg.session_id:
                        self.session_id = msg.session_id
                        self.session_id_changed_at = int(time.time())
                        self._save_session()
                        self._save_session_to_redis()
                        logger.info(f"Session ID saved: {self.session_id[:8]}...")
                    if hasattr(msg, 'total_cost_usd') and msg.total_cost_usd is not None:
                        self.last_cost_usd = msg.total_cost_usd
                        self.total_cost_usd += msg.total_cost_usd
                    if hasattr(msg, 'usage') and msg.usage:
                        self.last_usage = msg.usage
                    self.last_duration_ms = getattr(msg, 'duration_ms', 0) or 0
                    self.last_num_turns = getattr(msg, 'num_turns', 0) or 0
                    self.last_stop_reason = getattr(msg, 'stop_reason', None)
                    dur_s = self.last_duration_ms / 1000
                    logger.info(f"Result: {dur_s:.1f}s, {self.last_num_turns} turns, stop={self.last_stop_reason}, cost=${self.last_cost_usd or 0:.4f}")
                    if msg.is_error and msg.result:
                        yield {"type": "error", "content": str(msg.result)}
                    self._expected_results -= 1
                    if self._expected_results <= 0:
                        break
                    else:
                        yield {"type": "turn_done"}
                elif isinstance(msg, StreamEvent):
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
                    logger.info(f"Rate limit: {rl.status} ({rl.rate_limit_type}) util={rl.utilization}")
        except Exception as e:
            if self.session_id and ("No conversation found" in str(e) or "exit code 1" in str(e)):
                logger.warning("Session %s failed (%s), invalidating and retrying", self.session_id[:8], type(e).__name__)
                self._invalidate_session()
                self._connected = False
                self._client = None
                async for chunk in self.send_message(text):
                    yield chunk
                return
            logger.error(f"SDK error: {e}", exc_info=True)
            self._connected = False
            self._client = None
            yield {"type": "error", "content": str(e)}
        finally:
            self._is_processing = False

    async def inject(self, text: str) -> bool:
        if not (self._client and self._connected and self._is_processing):
            return False
        try:
            await self._client.query(text)
            self._expected_results += 1
            logger.info(f"Injected (expect {self._expected_results} results): {text[:80]}...")
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

    async def set_model_live(self, model: str):
        self.model = model
        if self._client and self._connected:
            try:
                await self._client.set_model(model)
                logger.info(f"Model changed live to {model}")
            except Exception as e:
                logger.error(f"set_model error: {e}")

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
        logger.info("Session reset (cleared session_id + redis, disconnect awaited)")

    def reset(self):
        self._invalidate_session()
        self._last_ctx_usage = None
        self.reconnect()
        logger.info("Session reset (cleared session_id + redis)")

    async def _safe_disconnect(self, client=None):
        client = client or self._client
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass
