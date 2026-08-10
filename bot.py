"""Kesha Telegram Bot — bootstrap, bot/dp creation, main()."""

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from config import (
    ALLOWED,
    DEBOUNCE_SEC,
    GREET_FLAG,
    MODEL,
    RUNTIME,
    STRINGS,
    TOKEN,
    WORK_DIR,
    logger,
    load_system_prompt,
)
from chat_state import ChatRegistry
from claude_session import ClaudeSession
from kesha_tools import (
    get_current_chat,
    kesha_server,
    register_bridge_tools,
    set_bot_ref,
    set_current_chat,
)
from tool_bridge import ToolBridge, issue_token
import reminders as _reminders
import compact as _compact
import inbox_server as _inbox
import rag as _rag

import file_access as _file_access
import telegram_io as _tio
import media as _media
import response_stream as _rs
import handlers as _handlers

# Wire up bot object via set_bot() late binding after bot is created
_tg_proxy = os.getenv("TG_PROXY") or os.getenv("HTTPS_PROXY") or None
_bot_session = AiohttpSession(timeout=120, proxy=_tg_proxy)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN), session=_bot_session)
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
_rag_executor: Optional[ThreadPoolExecutor] = None
_rag_read_executor: Optional[ThreadPoolExecutor] = None


async def main():
    global BOT_START_TIME, _singleton_lock_fp, registry

    _singleton_lock_fp = _acquire_singleton_lock()
    tool_bridge = ToolBridge(issue_token(), get_current_chat)
    register_bridge_tools(tool_bridge)
    await tool_bridge.start()
    BOT_START_TIME = time.time()
    from message_log import get_db as _msg_db
    activity_store = _msg_db()

    registry = ChatRegistry(
        bot=bot,
        mcp_config=_mcp_config,
        system_prompt=_system_prompt,
        model=MODEL,
        debounce_sec=DEBOUNCE_SEC,
        ask_fn=_rs._ask,
        set_current_chat_fn=set_current_chat,
        get_lazy_block_fn=_reminders.get_lazy_block_for_prompt,
        compact_session_fn=_compact.compact_session,
        activity_store=activity_store,
        work_dir=WORK_DIR,
        runtime=RUNTIME,
    )

    # Wire registry to response_stream and handlers
    _rs.set_registry(registry)
    _handlers.set_registry(registry)
    _handlers.set_uptime_fn(uptime_str)
    await registry.start_auto_compact()

    # Register all handlers
    _handlers.register(dp)

    _inbox.set_refs(bot, registry)
    await _inbox.start_inbox_server()

    _file_access.ensure_roots()
    _media.cleanup_media()
    _media.cleanup_logs()
    asyncio.create_task(_media.daily_cleanup_loop())

    # RAG semantic memory — два потока: write (index/backfill, RW conn) и read (search, RO conn).
    # WAL → search не ждёт backfill (иначе search висел 300с в очереди за embed-батчами). #10-opt
    global _rag_executor, _rag_read_executor
    _rag_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-w")
    _rag_read_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-r")
    _rag.set_executor(_rag_executor, _rag_read_executor)
    loop = asyncio.get_running_loop()
    rag_queue: asyncio.Queue = asyncio.Queue()

    async def _rag_worker():
        while True:
            mid, cid, role, content = await rag_queue.get()
            try:
                await _rag.run(loop, "index_message", mid, cid, role, content)
            except Exception as e:
                logger.error(f"RAG index failed (id={mid}): {e}")
            finally:
                rag_queue.task_done()
    asyncio.create_task(_rag_worker())

    def _enqueue_index(mid, cid, role, c):
        loop.call_soon_threadsafe(rag_queue.put_nowait, (mid, cid, role, c))
    _msg_db().set_on_message(_enqueue_index)
    asyncio.ensure_future(_rag.run(loop, "backfill"))

    # RAG file indexing (task #10) — knowledge base (cog-second-brain) → same rag_executor.
    # backfill on startup (non-blocking) + live watchfiles watcher.
    asyncio.ensure_future(_rag.run(loop, "backfill_files"))

    async def _file_watcher():
        from watchfiles import awatch, Change
        root = _rag.KNOWLEDGE_DIR
        try:
            async for changes in awatch(str(root)):
                for change, raw_path in changes:
                    rel = _rag.file_change_target(raw_path, root)
                    if rel is None:
                        continue
                    deleted = change == Change.deleted
                    try:
                        await _rag.run(loop, "apply_file_change", deleted, rel, raw_path)
                    except Exception as e:
                        logger.error(f"RAG file change failed ({rel}): {e}")
        except Exception as e:
            logger.error(f"RAG file watcher stopped: {e}", exc_info=True)
    asyncio.create_task(_file_watcher())

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

    should_greet_llm = GREET_FLAG.exists()
    if should_greet_llm:
        GREET_FLAG.unlink(missing_ok=True)
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
        await tool_bridge.stop()
        await registry.shutdown()
        if _rag_executor:
            _rag_executor.shutdown(wait=False)
        if _rag_read_executor:
            _rag_read_executor.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
