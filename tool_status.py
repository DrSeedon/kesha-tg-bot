"""Live tool status bubble for Telegram — one persistent message with growing tool log."""

import asyncio
import logging
import re
import time
from pathlib import Path
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
    "Skill": "🧩",
    "TodoWrite": "📝",
    "NotebookEdit": "📓",
    "run_command": "🖥",
    "read_file": "📖",
    "view_file": "📖",
    "write_file": "✏️",
    "edit_file": "✏️",
    "replace_file_content": "✏️",
    "multi_replace_file_content": "✏️",
    "grep_search": "🔎",
    "find_by_name": "🔎",
    "list_dir": "🔎",
}

# Icons per MCP server name (extracted from mcp__<server>__<tool> or mcp_<server>_...)
MCP_SERVER_ICONS = {
    "mailru": "📧",
    "gmail": "📧",
    "websearch": "🌐",
    "kesha": "🦜",
    "orchestra": "🎼",
    "yougile": "📋",
    "pandoc": "📄",
    "mcp-pandoc": "📄",
    "aperant": "🏠",
    "github": "🐙",
    "github-actions": "⚙️",
    "gpb": "🏦",
    "ozon": "🛒",
    "hub-images": "🖼",
}
_ACP_MCP_HEX_RE = re.compile(r"^mcp_([a-z0-9-]+?)_[0-9a-f]{8}_(.+)$")
_ACP_MCP_PLAIN_RE = re.compile(r"^mcp_([a-z0-9-]+?)_(.+)$")

EDIT_INTERVAL = 1.0         # min seconds between message edits (TG limit ~1 edit/s per message)
TICK_INTERVAL = 1.0         # how often the timer refreshes the display
STALL_HINT_AFTER = 60       # seconds — show "⏱ still working" hint when current tool runs longer
MAX_HINT_LEN = 120
MAX_VISIBLE_TOOLS = 20      # bubble lives the whole turn now — keep it under the 4096 TG limit
MAX_BUBBLE_LEN = 3900


def _parse_mcp_tool(name: str) -> tuple[str, str] | None:
    clean = " ".join(str(name).split())
    if clean.startswith("Run "):
        clean = clean[4:].strip()
    if clean.startswith("mcp__"):
        parts = clean.split("__", 2)
        if len(parts) >= 3:
            return parts[1], parts[2]
        if len(parts) == 2:
            return parts[1], clean
        return None
    if clean.startswith("mcp_"):
        m = _ACP_MCP_HEX_RE.match(clean) or _ACP_MCP_PLAIN_RE.match(clean)
        if m:
            return m.group(1), m.group(2)
    return None


def _tool_icon(name: str) -> str:
    parsed = _parse_mcp_tool(name)
    if parsed is not None:
        return MCP_SERVER_ICONS.get(parsed[0], "🔌")
    clean = " ".join(str(name).split())
    if clean.startswith(("File ", "Read ", "View ")):
        return "📖"
    if clean.startswith(("Edit ", "Write ")):
        return "✏️"
    if clean.startswith(("Grep ", "Find ", "Search ")):
        return "🔎"
    if clean.startswith("Run "):
        return "🖥"
    for key, icon in TOOL_ICONS.items():
        if clean == key or clean.startswith(key):
            return icon
    return "🔧"


def _tool_short_name(name: str) -> str:
    """Format MCP and ACP tool titles concisely onto a single readable line."""
    parsed = _parse_mcp_tool(name)
    if parsed is not None:
        return parsed[1]
    clean = " ".join(str(name).split())
    if clean.startswith("File "):
        target = clean[5:].strip().split(" ", 1)[0]
        return Path(target).name or clean
    if clean.startswith("Run "):
        cmd = clean[4:].strip()
        return cmd if len(cmd) <= 60 else cmd[:59] + "…"
    return clean


def _escape_md(s: str) -> str:
    import re
    return re.sub(r'([*_`\[])', r'\\\1', s)


def _format_hint(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    # "skill" first: the Skill tool carries the skill name there, and its other
    # keys ("args") would otherwise hide which skill is actually running.
    for key in ("skill", "command", "file_path", "path", "pattern", "query", "prompt", "url", "description"):
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
        await self._render(force=False)
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

    async def complete_current(self):
        """Stop timing the active tool without closing the whole status log."""
        if self._current_idx is None:
            return
        current = self.tools[self._current_idx]
        if current["end"] is None:
            current["end"] = time.time()
        self._current_idx = None
        await self._render(force=False)

    def _render_text(self, final: bool = False, failed: bool = False) -> str:
        now = time.time()
        if failed:
            header = "🤖 *Прервано:*"
        else:
            header = "🤖 *Сделано:*" if final else "🤖 *Работаю...*"
        lines = [header]
        hidden = max(0, len(self.tools) - MAX_VISIBLE_TOOLS)
        if hidden:
            lines.append(f"… ещё {hidden}")
        for i, t in enumerate(self.tools):
            if i < hidden:
                continue
            is_current = (i == self._current_idx) and not final and t["end"] is None
            end = t["end"] if t["end"] is not None else now
            dur = int(end - t["start"])
            if is_current:
                marker = "⏳"
                if dur >= STALL_HINT_AFTER:
                    marker = "⏱"
                lines.append(f"{marker} {t['icon']} {_escape_md(t['name'])}{t['hint']} · {dur}s")
            elif failed and i == self._current_idx:
                lines.append(f"⚠️ {t['icon']} {_escape_md(t['name'])}{t['hint']} · {dur}s")
            else:
                lines.append(f"✅ {t['icon']} {_escape_md(t['name'])}{t['hint']} · {dur}s")
        return "\n".join(lines)[:MAX_BUBBLE_LEN]

    async def _render(self, force: bool = False):
        if self._stopped:
            return
        now = time.time()
        if now < self._flood_until:
            return
        if not force and (now - self._last_edit_ts) < EDIT_INTERVAL:
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
                self._handle_tg_error(e, now, text, label="initial send")
            return
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=self.status_msg.message_id, parse_mode="Markdown"
            )
            self._last_text = text
            self._last_edit_ts = now
        except Exception as e:
            self._handle_tg_error(e, now, text, label="edit")

    def _handle_tg_error(self, e: Exception, now: float, text: str, *, label: str) -> None:
        err = str(e)
        if "Flood control" in err or "retry after" in err.lower():
            m = re.search(r"retry after (\d+)", err, re.IGNORECASE)
            wait = int(m.group(1)) if m else 15
            self._flood_until = now + wait + 1
            logger.info(f"ToolStatus flood control, pausing edits for {wait}s")
        elif "message is not modified" in err:
            self._last_text = text
            self._last_edit_ts = now
        else:
            logger.debug(f"ToolStatus {label} error: {e}")

    async def finalize(self) -> Optional[int]:
        """Stop ticker, mark all tools done, render final state. Returns status msg_id or None."""
        self._stopped = True
        now = time.time()
        if self._current_idx is not None and self.tools and self.tools[self._current_idx]["end"] is None:
            self.tools[self._current_idx]["end"] = now
        self._current_idx = None
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            # Тикер, отменённый до первого запуска, не успевает поймать
            # CancelledError у себя внутри — она BaseException и пролетала
            # мимо `except Exception`, обрывая весь ответ.
            try:
                await self._tick_task
            except (Exception, asyncio.CancelledError):
                pass
        self._tick_task = None
        if not self.tools or self.status_msg is None:
            return self.status_msg.message_id if self.status_msg else None
        if now < self._flood_until:
            return self.status_msg.message_id
        final_text = self._render_text(final=True)
        if final_text != self._last_text:
            try:
                await self.bot.edit_message_text(
                    final_text, chat_id=self.chat_id, message_id=self.status_msg.message_id, parse_mode="Markdown"
                )
                self._last_text = final_text
            except Exception as e:
                self._handle_tg_error(e, now, final_text, label="finalize edit")
        return self.status_msg.message_id

    async def fail(self) -> Optional[int]:
        """Stop ticker and mark the active tool interrupted, never successful."""
        self._stopped = True
        now = time.time()
        if self._current_idx is not None and self.tools and self.tools[self._current_idx]["end"] is None:
            self.tools[self._current_idx]["end"] = now
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            # Тикер, отменённый до первого запуска, не успевает поймать
            # CancelledError у себя внутри — она BaseException и пролетала
            # мимо `except Exception`, обрывая весь ответ.
            try:
                await self._tick_task
            except (Exception, asyncio.CancelledError):
                pass
        self._tick_task = None
        if not self.tools or self.status_msg is None:
            return self.status_msg.message_id if self.status_msg else None
        if now < self._flood_until:
            return self.status_msg.message_id
        final_text = self._render_text(failed=True)
        if final_text != self._last_text:
            try:
                await self.bot.edit_message_text(
                    final_text,
                    chat_id=self.chat_id,
                    message_id=self.status_msg.message_id,
                    parse_mode="Markdown",
                )
                self._last_text = final_text
            except Exception as e:
                self._handle_tg_error(e, now, final_text, label="fail edit")
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
