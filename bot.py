"""Kesha Telegram Bot — bootstrap, bot/dp creation, main()."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import (
    ALLOWED,
    AUTO_COMPACT_PCT,
    DEBOUNCE_SEC,
    GREET_FLAG,
    MODEL,
    STRINGS,
    TOKEN,
    WORK_DIR,
    logger,
    load_system_prompt,
)
from chat_state import ChatRegistry
from claude_session import ClaudeSession
from kesha_tools import kesha_server, set_bot_ref, set_current_chat
import reminders as _reminders
import compact as _compact

import telegram_io as _tio
import media as _media
import response_stream as _rs
import handlers as _handlers

# Wire up bot object via set_bot() late binding after bot is created
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

set_bot_ref(sys.modules[__name__])

# Pass bot to submodules
_tio.set_bot(bot)
_media.set_bot(bot)
_rs.set_bot(bot)
_handlers.set_bot(bot)


def _load_global_mcp() -> dict:
    servers = {"kesha": kesha_server}
    sources = [
        Path.home() / ".claude.json",
        Path.home() / ".claude" / "settings.json",
        Path(WORK_DIR) / ".mcp.json",
    ]
    for path in sources:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for name, cfg in data.get("mcpServers", {}).items():
                    if name not in servers:
                        servers[name] = cfg
            except Exception:
                pass
    logger.info(f"MCP servers loaded: {list(servers.keys())}")
    return servers


_mcp_config = _load_global_mcp()
_system_prompt = load_system_prompt()

# ChatRegistry — initialized in main(), used by all handlers
registry: Optional[ChatRegistry] = None


def get_session(chat_id: int) -> ClaudeSession:
    """Convenience accessor for tools/reminders that need the raw ClaudeSession."""
    if registry is None:
        raise RuntimeError("ChatRegistry not initialized — call main() first")
    return registry.get(chat_id).session


BOT_START_TIME = None


def uptime_str() -> str:
    if not BOT_START_TIME:
        return "unknown"
    delta = int(time.time() - BOT_START_TIME)
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _acquire_singleton_lock():
    import fcntl
    lock_path = Path(__file__).parent / "storage" / "bot.pid.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error(f"Another kesha-bot instance is already running (lock: {lock_path}). Exiting.")
        sys.exit(1)
    lock_fp.write(str(os.getpid()))
    lock_fp.flush()
    return lock_fp


_singleton_lock_fp = None


async def main():
    global BOT_START_TIME, _singleton_lock_fp, registry

    _singleton_lock_fp = _acquire_singleton_lock()
    BOT_START_TIME = time.time()

    registry = ChatRegistry(
        bot=bot,
        mcp_config=_mcp_config,
        system_prompt=_system_prompt,
        model=MODEL,
        debounce_sec=DEBOUNCE_SEC,
        auto_compact_pct=AUTO_COMPACT_PCT,
        ask_fn=_rs._ask,
        set_current_chat_fn=set_current_chat,
        get_lazy_block_fn=_reminders.get_lazy_block_for_prompt,
        compact_session_fn=_compact.compact_session,
        maybe_auto_compact_fn=_compact.maybe_auto_compact,
        work_dir=WORK_DIR,
    )

    # Wire registry to response_stream and handlers
    _rs.set_registry(registry)
    _handlers.set_registry(registry)
    _handlers.set_uptime_fn(uptime_str)

    # Register all handlers
    _handlers.register(dp)

    _media.cleanup_media()
    _media.cleanup_logs()
    asyncio.create_task(_media.daily_cleanup_loop())
    await _handlers.set_commands(bot)
    logger.info(f"Kesha bot | CWD={WORK_DIR} | Model={MODEL}")
    logger.info(f"Allowed: {ALLOWED or 'all'}")

    async def _urgent_llm_handler(chat_id: int, prompt: str):
        from datetime import datetime as dt, timezone as tz, timedelta as td
        krsk = tz(td(hours=7))
        now_str = dt.now(tz=krsk).strftime("%Y-%m-%d %H:%M %z")
        full_prompt = f"[{now_str}] " + prompt
        await registry.get(chat_id).run_urgent_prompt(full_prompt)

    _reminders.set_urgent_llm_handler(_urgent_llm_handler)

    try:
        await _reminders.deliver_missed_on_startup(bot, get_session, ALLOWED)
    except Exception as e:
        logger.error(f"Missed reminders delivery failed: {e}", exc_info=True)
    asyncio.create_task(_reminders.reminder_loop(bot, get_session, ALLOWED))

    logger.info(f"Greet flag path: {GREET_FLAG}, exists: {GREET_FLAG.exists()}")
    should_greet_llm = GREET_FLAG.exists()
    if should_greet_llm:
        GREET_FLAG.unlink(missing_ok=True)
        logger.info("Greet flag found and deleted — will send LLM greeting")
    for uid in ALLOWED:
        try:
            await bot.send_message(uid, STRINGS["ru"]["started"])
        except Exception:
            pass
        if should_greet_llm:
            asyncio.create_task(_urgent_llm_handler(uid,
                "[BOT RESTARTED] You just restarted after applying code changes. Write a brief in-character message — confirm you're back and what was updated. 1-2 sentences max."))

    try:
        await dp.start_polling(bot)
    finally:
        await registry.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
