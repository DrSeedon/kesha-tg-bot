"""Streaming response handler: _ask() — streams Claude response to Telegram."""

import json
import time
from typing import Optional

from aiogram import types
from aiogram.methods import SendMessageDraft

import config as _config
from config import MAX_RETRIES, STRINGS, TG_MSG_LIMIT, logger, t as _t_cfg
from telegram_io import (
    _next_draft_id,
    _send_safe,
    split_msg,
    typing_loop,
)
from tool_status import ToolStatusTracker

import asyncio

STREAM_DRAFT_INTERVAL = 0.3

_bot = None
_registry = None


def set_bot(bot_instance) -> None:
    global _bot
    _bot = bot_instance


def set_registry(registry_instance) -> None:
    global _registry
    _registry = registry_instance


def _get_session(chat_id: int):
    if _registry is None:
        raise RuntimeError("registry not set — call set_registry() first")
    return _registry.get(chat_id).session


async def _ask(message: Optional[types.Message], prompt: str, chat_id: int):
    """Stream a Claude response. message may be None for reminder turns (uses bot.send_message)."""
    cid = chat_id
    typer = asyncio.create_task(typing_loop(cid))
    retries = 0

    parts: list[str] = []
    has_deltas = False
    draft_id = _next_draft_id()
    last_draft_time = 0.0
    last_draft_text = ""
    draft_has_text = False
    flood_cooldown_until = 0.0
    finalized: list[int] = []

    status: Optional[ToolStatusTracker] = None

    async def _answer(text: str, **kwargs):
        if message is not None:
            return await message.answer(text, **kwargs)
        return await _bot.send_message(cid, text, **kwargs)

    async def _draft_update(final: bool = False):
        nonlocal last_draft_time, last_draft_text, flood_cooldown_until, draft_has_text
        text = "".join(parts)[:TG_MSG_LIMIT]
        if not text:
            return
        now = time.time()
        if not final:
            if now < flood_cooldown_until:
                return
            if (now - last_draft_time) < STREAM_DRAFT_INTERVAL:
                return
            if text == last_draft_text:
                return
        parse_mode = "Markdown" if final else None
        try:
            await _bot(SendMessageDraft(chat_id=cid, draft_id=draft_id, text=text, parse_mode=parse_mode))
            draft_has_text = True
            last_draft_text = text
        except Exception as e:
            err_str = str(e)
            if final and ("can't parse entities" in err_str or "parse" in err_str.lower()):
                try:
                    await _bot(SendMessageDraft(chat_id=cid, draft_id=draft_id, text=text, parse_mode=None))
                    draft_has_text = True
                    last_draft_text = text
                except Exception as e2:
                    logger.debug(f"Draft final plain fallback failed: {e2}")
            elif "Flood control" in err_str or "retry after" in err_str.lower():
                import re
                m = re.search(r'retry after (\d+)', err_str, re.IGNORECASE)
                wait_sec = int(m.group(1)) if m else 30
                flood_cooldown_until = now + wait_sec + 1
                logger.info(f"Draft flood control, pausing updates for {wait_sec}s")
            elif "message is not modified" in err_str:
                last_draft_text = text
            else:
                logger.debug(f"Draft update error: {e}")
        last_draft_time = now

    async def _finalize_text_block():
        nonlocal parts, has_deltas, draft_has_text, draft_id, last_draft_time, last_draft_text
        text = "".join(parts)
        if not text:
            return
        for p in split_msg(text):
            from aiogram.exceptions import TelegramRetryAfter
            for attempt in range(3):
                try:
                    m = await _answer(p)
                    if m:
                        finalized.append(m.message_id)
                    break
                except TelegramRetryAfter as e:
                    logger.warning(f"Flood control, retry after {e.retry_after}s (attempt {attempt+1})")
                    await asyncio.sleep(e.retry_after + 1)
                except Exception as e:
                    err_str = str(e)
                    if "can't parse" in err_str.lower() or "parse entities" in err_str.lower():
                        try:
                            m = await _answer(p, parse_mode=None)
                            if m:
                                finalized.append(m.message_id)
                        except Exception:
                            pass
                    else:
                        logger.error(f"_finalize_text_block error: {e}")
                    break
        draft_has_text = False
        draft_id = _next_draft_id()
        last_draft_time = 0.0
        last_draft_text = ""
        parts = []
        has_deltas = False

    async def _finalize_status():
        nonlocal status
        if status:
            mid = await status.finalize()
            if mid:
                finalized.append(mid)
            status = None

    while retries <= MAX_RETRIES:
        need_retry = False
        try:
            stream = _get_session(cid).send_message(prompt).__aiter__()
            while True:
                try:
                    chunk = await stream.__anext__()
                except StopAsyncIteration:
                    break
                cs = _registry.get(cid)
                if cs.should_stop():
                    if parts:
                        parts.append("\n\n_(stopped)_")
                    break
                ct = chunk["type"]
                if ct == "text_delta":
                    if status is not None:
                        await _finalize_status()
                    has_deltas = True
                    parts.append(chunk["content"])
                    await _draft_update()
                elif ct == "text" and not has_deltas:
                    if status is not None:
                        await _finalize_status()
                    parts.append(chunk["content"])
                    await _draft_update()
                elif ct == "tool":
                    tool_name = chunk.get("name", "?")
                    tool_input = chunk.get("input", {})
                    if parts:
                        await _finalize_text_block()
                    if status is None:
                        status = ToolStatusTracker(_bot, message, cid)
                    try:
                        _ti_short = json.dumps(tool_input, ensure_ascii=False)[:400]
                    except Exception:
                        _ti_short = str(tool_input)[:400]
                    logger.info(f"Chat {cid} tool: {tool_name} input={_ti_short}")
                    await status.add_tool(tool_name, tool_input)
                elif ct == "turn_done":
                    if parts:
                        await _finalize_text_block()
                    await _finalize_status()
                elif ct == "error":
                    err = chunk["content"]
                    if "session" in err.lower() or "process" in err.lower():
                        logger.warning(f"Session error, reconnecting: {err}")
                        _get_session(cid).reconnect()
                        retries += 1
                        if retries <= MAX_RETRIES and not finalized:
                            if message is not None:
                                await _send_safe(message, _t_cfg(message, "reconnecting", n=retries))
                            parts.clear()
                            has_deltas = False
                            if status:
                                if status.tools:
                                    await status.finalize()
                                else:
                                    await status.cancel_empty()
                                status = None
                            need_retry = True
                            break
                    parts.append(f"Error: {err}")
            if not need_retry:
                break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            retries += 1
            if retries <= MAX_RETRIES:
                _get_session(cid).reconnect()
                if message is not None:
                    await _send_safe(message, _t_cfg(message, "error_retry", n=retries))
            else:
                parts.append(f"Error: {e}")
                break

    typer.cancel()

    text = "".join(parts)
    logger.info(f"Chat {cid}: response {len(text)} chars, finalized={len(finalized)}, tools={len(status.tools) if status else 0}, draft_hanging={draft_has_text}")
    if _config.DEBUG:
        logger.debug(f"Chat {cid} full response: {text[:500]}")

    if parts:
        await _finalize_text_block()

    if status is not None:
        if status.tools:
            mid = await status.finalize()
            if mid:
                finalized.append(mid)
        else:
            await status.cancel_empty()
        status = None

    if not text and not finalized:
        await _answer(STRINGS["ru"]["empty"] if message is None else _t_cfg(message, "empty"))
