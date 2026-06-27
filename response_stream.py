"""Streaming response handler: _ask() — streams Claude response to Telegram."""

import asyncio
import contextlib
import json
import time
from typing import Optional

from aiogram import types
from aiogram.exceptions import TelegramRetryAfter

import config as _config
from config import MAX_RETRIES, STRINGS, TG_MSG_LIMIT, logger, t as _t_cfg
from telegram_io import (
    _send_safe,
    split_msg,
    typing_loop,
)
from tool_status import ToolStatusTracker

STREAM_EDIT_INTERVAL = 1.0  # TG edit limit ~20/min → 1s safe minimum

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


from telegramify_markdown import convert as _md_convert, split_entities as _split_entities


async def _ask(message: Optional[types.Message], prompt: str, chat_id: int):
    """Stream a Claude response. message may be None for reminder turns (uses bot.send_message)."""
    cid = chat_id
    typer = asyncio.create_task(typing_loop(cid))
    try:
        return await _ask_inner(message, prompt, cid, typer)
    except asyncio.CancelledError:
        logger.warning(f"Chat {cid}: _ask cancelled (CancelledError)")
        raise
    finally:
        typer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typer


async def _stop_typer(typer: asyncio.Task) -> None:
    if not typer.done():
        typer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typer


async def _ask_inner(message, prompt, cid, typer):
    retries = 0

    parts: list[str] = []
    has_deltas = False
    current_msg_id: Optional[int] = None  # ID of the live message being edited
    last_edit_time = 0.0
    last_edit_text = ""
    edit_flood_until = 0.0
    finalized: list[int] = []

    status: Optional[ToolStatusTracker] = None

    async def _answer(text: str, **kwargs):
        if message is not None:
            return await message.answer(text, **kwargs)
        return await _bot.send_message(cid, text, **kwargs)

    async def _edit_update():
        nonlocal current_msg_id, last_edit_time, last_edit_text, edit_flood_until
        text = "".join(parts)
        if not text:
            return
        now = time.time()

        # First chunk — send immediately without throttle
        if current_msg_id is None:
            try:
                m = await _answer(text[:TG_MSG_LIMIT], parse_mode=None)
                if m:
                    current_msg_id = m.message_id
                    last_edit_text = text
                    last_edit_time = now
            except Exception as e:
                logger.debug(f"Edit stream: initial send failed: {e}")
            return

        # Subsequent edits — apply throttle and flood control
        if now < edit_flood_until:
            return
        if (now - last_edit_time) < STREAM_EDIT_INTERVAL:
            return
        if text == last_edit_text:
            return

        try:
            await _bot.edit_message_text(
                text[:TG_MSG_LIMIT], chat_id=cid, message_id=current_msg_id, parse_mode=None
            )
            last_edit_text = text
            last_edit_time = now
        except Exception as e:
            err = str(e)
            if "Flood control" in err or "retry after" in err.lower():
                import re
                m = re.search(r'retry after (\d+)', err, re.IGNORECASE)
                wait_sec = int(m.group(1)) if m else 30
                edit_flood_until = now + wait_sec + 1
                logger.info(f"Edit flood control, pausing updates for {wait_sec}s")
            elif "message is not modified" in err:
                last_edit_text = text
                last_edit_time = now
            else:
                logger.debug(f"Edit update error: {e}")

    async def _finalize_text_block():
        nonlocal parts, has_deltas, current_msg_id, last_edit_time, last_edit_text
        raw = "".join(parts)
        if not raw:
            return
        await _stop_typer(typer)

        try:
            converted_text, entities = _md_convert(raw)
            chunks = _split_entities(converted_text, entities, TG_MSG_LIMIT)
        except Exception as e:
            logger.warning(f"telegramify_markdown convert failed: {e}, sending plain")
            chunks = [(p, []) for p in split_msg(raw)]

        for i, (chunk_text, chunk_ents) in enumerate(chunks):
            if not chunk_text:
                continue
            ent_dicts = [e.to_dict() for e in chunk_ents] if chunk_ents else None

            if i == 0 and current_msg_id is not None:
                # Final edit of the live message
                for attempt in range(3):
                    try:
                        await _bot.edit_message_text(
                            chunk_text, chat_id=cid, message_id=current_msg_id,
                            parse_mode=None, entities=ent_dicts
                        )
                        finalized.append(current_msg_id)
                        break
                    except TelegramRetryAfter as e:
                        logger.warning(f"Flood control (finalize edit), retry after {e.retry_after}s")
                        await asyncio.sleep(e.retry_after + 1)
                    except Exception as e:
                        err = str(e)
                        if "message is not modified" in err:
                            finalized.append(current_msg_id)
                        else:
                            logger.error(f"_finalize edit error: {e}")
                            # Fallback: send as new message
                            try:
                                m = await _answer(chunk_text, parse_mode=None, entities=ent_dicts)
                                if m:
                                    finalized.append(m.message_id)
                            except Exception:
                                pass
                        break
            else:
                # Overflow chunks (>4096) or first chunk when no live message exists
                for attempt in range(3):
                    try:
                        m = await _answer(chunk_text, parse_mode=None, entities=ent_dicts)
                        if m:
                            finalized.append(m.message_id)
                        break
                    except TelegramRetryAfter as e:
                        logger.warning(f"Flood control (finalize send), retry after {e.retry_after}s (attempt {attempt+1})")
                        await asyncio.sleep(e.retry_after + 1)
                    except Exception as e:
                        logger.error(f"_finalize_text_block error: {e}")
                        try:
                            m = await _answer(chunk_text, parse_mode=None)
                            if m:
                                finalized.append(m.message_id)
                        except Exception:
                            pass
                        break

        current_msg_id = None
        last_edit_time = 0.0
        last_edit_text = ""
        parts = []
        has_deltas = False

    async def _finalize_status():
        nonlocal status
        if status:
            mid = await status.finalize()
            if mid:
                finalized.append(mid)
            status = None

    CHUNK_TIMEOUT_TEXT = 120
    CHUNK_TIMEOUT_TOOL = 300

    while retries <= MAX_RETRIES:
        need_retry = False
        _last_chunk_type = None
        logger.info(f"Chat {cid}: retry loop iteration retries={retries}/{MAX_RETRIES}")
        try:
            stream = _get_session(cid).send_message(prompt).__aiter__()
            while True:
                try:
                    _in_tool_phase = _last_chunk_type in ("tool", "result")
                    timeout = CHUNK_TIMEOUT_TOOL if _in_tool_phase else CHUNK_TIMEOUT_TEXT
                    chunk = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"Chat {cid}: stream stall after {timeout}s (last_chunk={_last_chunk_type}), finalizing")
                    if not parts and not finalized and retries < MAX_RETRIES:
                        logger.info(f"Chat {cid}: stall on empty response, retrying ({retries+1}/{MAX_RETRIES})")
                        _get_session(cid).reconnect()
                        retries += 1
                        need_retry = True
                        try:
                            if message is not None:
                                await _send_safe(message, _t_cfg(message, "reconnecting", n=retries))
                            if status:
                                if status.tools:
                                    await status.finalize()
                                else:
                                    await status.cancel_empty()
                                status = None
                        except asyncio.CancelledError:
                            raise
                        logger.info(f"Chat {cid}: breaking inner loop for retry")
                        break
                    if parts:
                        parts.append("\n\n⚠️ _ответ прервался (stream timeout)_")
                    elif status is not None:
                        await _finalize_status()
                        if message is not None:
                            await _send_safe(message, "⚠️ _ответ прервался (stream timeout)_")
                    _get_session(cid).reconnect()
                    break
                except StopAsyncIteration:
                    break
                cs = _registry.get(cid)
                if cs.should_stop():
                    if parts:
                        parts.append("\n\n_(stopped)_")
                    break
                ct = chunk["type"]
                _last_chunk_type = ct
                if ct == "text_delta":
                    if status is not None:
                        await _finalize_status()
                    has_deltas = True
                    parts.append(chunk["content"])
                    await _edit_update()
                elif ct == "text" and not has_deltas:
                    if status is not None:
                        await _finalize_status()
                    parts.append(chunk["content"])
                    await _edit_update()
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
                            need_retry = True
                            try:
                                if message is not None:
                                    await _send_safe(message, _t_cfg(message, "reconnecting", n=retries))
                                parts.clear()
                                has_deltas = False
                                current_msg_id = None  # EC-6: reset live message on reconnect
                                last_edit_text = ""
                                if status:
                                    if status.tools:
                                        await status.finalize()
                                    else:
                                        await status.cancel_empty()
                                    status = None
                            except asyncio.CancelledError:
                                raise
                            break
                    parts.append(f"Error: {err}")
            if not need_retry:
                logger.info(f"Chat {cid}: inner loop done, no retry needed")
                break
            logger.info(f"Chat {cid}: need_retry=True, continuing outer loop (retries={retries})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Chat {cid}: outer exception in retry loop (retries={retries}): {type(e).__name__}: {e}", exc_info=True)
            retries += 1
            if retries <= MAX_RETRIES:
                _get_session(cid).reconnect()
                current_msg_id = None
                last_edit_text = ""
                if message is not None:
                    await _send_safe(message, _t_cfg(message, "error_retry", n=retries))
                logger.info(f"Chat {cid}: error retry, continuing (retries={retries})")
            else:
                parts.append(f"Error: {e}")
                logger.info(f"Chat {cid}: max retries reached, giving up")
                break

    text = "".join(parts)
    logger.info(f"Chat {cid}: response {len(text)} chars, finalized={len(finalized)}, tools={len(status.tools) if status else 0}")
    if text:
        try:
            from message_log import get_db as _get_msg_db
            _get_msg_db().log_assistant(cid, text)
        except Exception:
            pass
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
