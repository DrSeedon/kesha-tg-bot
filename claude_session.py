"""Claude session via claude-agent-sdk with resume support."""

import logging
from typing import AsyncGenerator, Optional

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

logger = logging.getLogger(__name__)


class ClaudeSession:
    def __init__(self, cwd: str, model: str = "claude-sonnet-4-6", system_prompt: str = ""):
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt
        self.session_id: Optional[str] = None

    async def send_message(self, text: str) -> AsyncGenerator[dict, None]:
        options = ClaudeAgentOptions(
            model=self.model,
            cwd=self.cwd,
            max_turns=25,
            permission_mode="bypassPermissions",
        )

        if self.system_prompt:
            options.system_prompt = self.system_prompt

        if self.session_id:
            options.resume = self.session_id
            logger.info(f"Resuming session {self.session_id[:8]}...")
        else:
            logger.info("Starting new session...")

        logger.info(f"Prompt: {text[:150]}...")

        try:
            async for msg in query(prompt=text, options=options):
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
                        logger.info(f"Session ID saved: {self.session_id[:8]}")
                    if msg.is_error and msg.result:
                        yield {"type": "error", "content": str(msg.result)}
        except Exception as e:
            logger.error(f"SDK error: {e}", exc_info=True)
            yield {"type": "error", "content": str(e)}

    def reset(self):
        self.session_id = None
        logger.info("Session reset")
