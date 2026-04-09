"""Claude session via ClaudeSDKClient — persistent connection with interrupt support."""

import asyncio
import logging
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
)

logger = logging.getLogger(__name__)

SESSION_FILE = Path("./storage/session_id")


class ClaudeSession:
    def __init__(self, cwd: str, model: str = "claude-sonnet-4-6",
                 system_prompt: str = "",
                 mcp_servers: dict[str, McpSdkServerConfig] | None = None):
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt
        self.mcp_servers = mcp_servers or {}
        self.session_id: Optional[str] = self._load_session()
        self.last_cost_usd: Optional[float] = None
        self.total_cost_usd: float = 0.0
        self.last_usage: Optional[dict[str, Any]] = None
        self.rate_limit: Optional[dict[str, Any]] = None
        self.last_duration_ms: int = 0
        self.last_num_turns: int = 0
        self.last_stop_reason: Optional[str] = None
        self._client: Optional[ClaudeSDKClient] = None
        self._connected = False
        self._got_result = True
        self.use_1m = True

    def _load_session(self) -> Optional[str]:
        if SESSION_FILE.exists():
            sid = SESSION_FILE.read_text().strip()
            if sid:
                logger.info(f"Loaded session from file: {sid[:8]}...")
                return sid
        return None

    def _save_session(self):
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(self.session_id or "")

    def _make_options(self) -> ClaudeAgentOptions:
        model = self.model
        if self.use_1m and "[1m]" not in model:
            model = f"{model}[1m]"
        options = ClaudeAgentOptions(
            model=model,
            cwd=self.cwd,
            max_turns=25,
            permission_mode="bypassPermissions",
            include_partial_messages=True,
        )
        if self.system_prompt:
            options.system_prompt = self.system_prompt
        if self.mcp_servers:
            options.mcp_servers = self.mcp_servers
        if self.session_id:
            options.resume = self.session_id
        return options

    async def _ensure_connected(self, prompt: str):
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        options = self._make_options()
        self._client = ClaudeSDKClient(options=options)

        if self.session_id:
            logger.info(f"Connecting with resume {self.session_id[:8]}...")
        else:
            logger.info("Connecting new session...")

        await self._client.connect(prompt)
        self._connected = True
        self._got_result = False

    async def send_message(self, text: str) -> AsyncGenerator[dict, None]:
        logger.info(f"Prompt: {text[:150]}...")

        try:
            await self._ensure_connected(text)

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
                        self._save_session()
                        logger.info(f"Session ID saved: {self.session_id[:8]}")
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
                    self._got_result = True
                    if msg.is_error and msg.result:
                        yield {"type": "error", "content": str(msg.result)}
                    break
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
            logger.error(f"SDK error: {e}", exc_info=True)
            self._connected = False
            self._client = None
            yield {"type": "error", "content": str(e)}

        if not self._got_result:
            logger.warning("No ResultMessage received — connection may be stale, marking for reconnect")
            self._connected = False

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
                return await self._client.get_context_usage()
            except Exception as e:
                logger.error(f"get_context_usage error: {e}")
        return None

    def reconnect(self):
        self._connected = False
        if self._client:
            asyncio.create_task(self._safe_disconnect())
            self._client = None
        logger.info("Session reconnecting (keeping session_id)")

    def reset(self):
        self.session_id = None
        self._save_session()
        self.reconnect()
        logger.info("Session reset (cleared session_id)")

    async def _safe_disconnect(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass
