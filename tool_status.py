"""Live tool status bubble for Telegram — one persistent message with growing tool log."""

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("kesha")

TOOL_ICONS = {
    "Bash": "🖥",
    "Read": "📖",
    "Write": "✏️",
    "Edit": "✏️",
    "Glob": "🔎",
    "Grep": "🔎",
    "WebSearch": "🌐",
    "WebFetch": "🌐",
    "Agent": "🤖",
    "Task": "🤖",
    "TodoWrite": "📝",
    "NotebookEdit": "📓",
}

# Icons per MCP server name (extracted from mcp__<server>__<tool>)
MCP_SERVER_ICONS = {
    "mailru": "📧",
    "websearch": "🌐",
    "kesha": "🦜",
    "yougile": "📋",
    "pandoc": "📄",
    "mcp-pandoc": "📄",
    "aperant": "🏠",
    "github": "🐙",
    "github-actions": "⚙️",
}

EDIT_INTERVAL = 1.0         # min seconds between message edits (TG limit ~1 edit/s per message)
TICK_INTERVAL = 1.0         # how often the timer refreshes the display
STALL_HINT_AFTER = 60       # seconds — show "⏱ still working" hint when current tool runs longer
MAX_HINT_LEN = 60


def _tool_icon(name: str) -> str:
    # MCP tool format: mcp__<server>__<action> — pick icon by server name,
    # not by substring match on the action (avoids mail_read → 📖 Read false positive)
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 2:
            server = parts[1]
            return MCP_SERVER_ICONS.get(server, "🔌")
        return "🔌"
    # Built-in tool: exact startswith match only
    for key, icon in TOOL_ICONS.items():
        if name == key or name.startswith(key):
            return icon
    return "🔧"


def _tool_short_name(name: str) -> str:
    """For MCP tools, strip the mcp__ prefix for readability: mcp__mailru__mail_read → mail_read"""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) >= 3:
            return parts[2]
    return name


def _escape_md(s: str) -> str:
    import re
    return re.sub(r'([*_`\[])', r'\\\1', s)


def _format_hint(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "query", "prompt", "url", "description"):
        if key in tool_input and tool_input[key]:
            val = str(tool_input[key])
            val = val.replace("\n", " ")
            if len(val) > MAX_HINT_LEN:
                val = val[:MAX_HINT_LEN] + "…"
            return f" {_escape_md(val)}"
    return ""


class ToolStatusTracker:
    """Maintains one TG message showing a live log of tool calls with timers."""

    def __init__(self, bot, message, chat_id: int):
        self.bot = bot
        self.message = message  # aiogram Message (for reply context)
        self.chat_id = chat_id
        self.status_msg = None  # TG message being edited
        self.tools: list[dict] = []  # [{name, icon, hint, start, end}]
        self._current_idx: Optional[int] = None
        self._last_edit_ts = 0.0
        self._last_text = ""
        self._tick_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._flood_until = 0.0

    async def add_tool(self, name: str, tool_input: Any):
        now = time.time()
        if self._current_idx is not None:
            self.tools[self._current_idx]["end"] = now
        icon = _tool_icon(name)
        display_name = _tool_short_name(name)
        hint = _format_hint(tool_input)
        self.tools.append({"name": display_name, "icon": icon, "hint": hint, "start": now, "end": None})
        self._current_idx = len(self.tools) - 1
        await self._render(force=True)
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._ticker())

    async def _ticker(self):
        try:
            while not self._stopped:
                await asyncio.sleep(TICK_INTERVAL)
                if self._stopped:
                    break
                await self._render(force=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"ToolStatus ticker error: {e}")

    def _render_text(self, final: bool = False) -> str:
        now = time.time()
        header = "🤖 *Сделано:*" if final else "🤖 *Работаю...*"
        lines = [header]
        for i, t in enumerate(self.tools):
            is_current = (i == self._current_idx) and not final and t["end"] is None
            end = t["end"] if t["end"] is not None else now
            dur = int(end - t["start"])
            if is_current:
                marker = "⏳"
                if dur >= STALL_HINT_AFTER:
                    marker = "⏱"
                lines.append(f"{marker} {t['icon']} {_escape_md(t['name'])}{t['hint']} · {dur}s")
            else:
                lines.append(f"✅ {t['icon']} {_escape_md(t['name'])}{t['hint']} · {dur}s")
        return "\n".join(lines)

    async def _render(self, force: bool = False):
        if self._stopped:
            return
        now = time.time()
        if not force:
            if now < self._flood_until:
                return
            if (now - self._last_edit_ts) < EDIT_INTERVAL:
                return
        text = self._render_text(final=False)
        if text == self._last_text and not force:
            return
        if self.status_msg is None:
            try:
                if self.message is not None:
                    self.status_msg = await self.message.answer(text, parse_mode="Markdown")
                else:
                    self.status_msg = await self.bot.send_message(self.chat_id, text, parse_mode="Markdown")
                self._last_text = text
                self._last_edit_ts = now
            except Exception as e:
                logger.debug(f"ToolStatus initial send error: {e}")
            return
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=self.status_msg.message_id, parse_mode="Markdown"
            )
            self._last_text = text
            self._last_edit_ts = now
        except Exception as e:
            err = str(e)
            if "Flood control" in err or "retry after" in err.lower():
                import re
                m = re.search(r"retry after (\d+)", err, re.IGNORECASE)
                wait = int(m.group(1)) if m else 15
                self._flood_until = now + wait + 1
                logger.info(f"ToolStatus flood control, pausing edits for {wait}s")
            elif "message is not modified" in err:
                self._last_text = text
                self._last_edit_ts = now
            else:
                logger.debug(f"ToolStatus edit error: {e}")

    async def finalize(self) -> Optional[int]:
        """Stop ticker, mark all tools done, render final state. Returns status msg_id or None."""
        self._stopped = True
        now = time.time()
        if self._current_idx is not None and self.tools and self.tools[self._current_idx]["end"] is None:
            self.tools[self._current_idx]["end"] = now
        self._current_idx = None
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except Exception:
                pass
        self._tick_task = None
        if not self.tools or self.status_msg is None:
            return self.status_msg.message_id if self.status_msg else None
        final_text = self._render_text(final=True)
        if final_text != self._last_text:
            try:
                await self.bot.edit_message_text(
                    final_text, chat_id=self.chat_id, message_id=self.status_msg.message_id, parse_mode="Markdown"
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    logger.debug(f"ToolStatus finalize edit error: {e}")
        return self.status_msg.message_id

    async def cancel_empty(self):
        """Delete status message if no tools were ever added (edge case)."""
        self._stopped = True
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
        if self.status_msg and not self.tools:
            try:
                await self.bot.delete_message(self.chat_id, self.status_msg.message_id)
            except Exception:
                pass
            self.status_msg = None
