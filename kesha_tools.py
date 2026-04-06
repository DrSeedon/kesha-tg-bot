"""Kesha self-configuration tools — injected as SDK MCP server."""

import asyncio
import logging

from claude_agent_sdk import tool, create_sdk_mcp_server

logger = logging.getLogger("kesha.tools")

_bot_ref = None


def set_bot_ref(bot_module):
    global _bot_ref
    _bot_ref = bot_module


@tool("set_model", "Change Claude model for this bot", {"model": str})
async def set_model(args):
    model = args["model"]
    _bot_ref.claude.model = model
    logger.info(f"Model changed to {model}")
    return {"content": [{"type": "text", "text": f"Model changed to {model}"}]}


@tool("set_debounce", "Change message debounce delay in seconds (0-30)", {"seconds": int})
async def set_debounce(args):
    sec = args["seconds"]
    if not 0 <= sec <= 30:
        return {"content": [{"type": "text", "text": "Debounce must be 0-30 seconds"}], "is_error": True}
    _bot_ref.DEBOUNCE_SEC = sec
    logger.info(f"Debounce changed to {sec}s")
    return {"content": [{"type": "text", "text": f"Debounce changed to {sec}s"}]}


@tool("toggle_debug", "Toggle debug logging on/off", {})
async def toggle_debug(args):
    _bot_ref.DEBUG = not _bot_ref.DEBUG
    _bot_ref.logger.setLevel(logging.DEBUG if _bot_ref.DEBUG else logging.INFO)
    state = "on" if _bot_ref.DEBUG else "off"
    logger.info(f"Debug toggled {state}")
    return {"content": [{"type": "text", "text": f"Debug is now {state}"}]}


@tool("get_bot_status", "Get current bot configuration and status", {})
async def get_bot_status(args):
    c = _bot_ref.claude
    rl = c.rate_limit
    if rl:
        util = rl.get('utilization')
        util_str = f" {int(util*100)}%" if util is not None else ""
        rl_str = f"{rl.get('status', '?')} ({rl.get('type', '?')}){util_str}"
    else:
        rl_str = "unknown"
    dur = f"{c.last_duration_ms/1000:.1f}s" if c.last_duration_ms else "n/a"
    status = (
        f"Model: {c.model}\n"
        f"Session: {c.session_id or 'none'}\n"
        f"Debounce: {_bot_ref.DEBOUNCE_SEC}s\n"
        f"Debug: {'on' if _bot_ref.DEBUG else 'off'}\n"
        f"CWD: {_bot_ref.WORK_DIR}\n"
        f"Rate limit: {rl_str}\n"
        f"Session cost: ${c.total_cost_usd:.4f}\n"
        f"Last response: {dur}, {c.last_num_turns} turns, stop={c.last_stop_reason}\n"
        f"Media files: {_bot_ref.media_count()}\n"
        f"Log size: {_bot_ref.log_size()}"
    )
    return {"content": [{"type": "text", "text": status}]}


@tool("restart_bot", "Restart the bot service (applies code changes)", {})
async def restart_bot(args):
    logger.info("Bot restart requested via tool")
    p = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "kesha-bot",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await p.communicate()
    if p.returncode != 0:
        return {"content": [{"type": "text", "text": f"Restart failed: {err.decode()}"}], "is_error": True}
    return {"content": [{"type": "text", "text": "Bot restarting..."}]}


kesha_server = create_sdk_mcp_server(
    name="kesha",
    tools=[set_model, set_debounce, toggle_debug, get_bot_status, restart_bot],
)
