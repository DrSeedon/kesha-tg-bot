"""Streaming response handler: _ask() — streams Claude response to Telegram."""

import asyncio
import contextlib
import json
import math
import re
from time import monotonic
from typing import Optional

from aiogram import types
from aiogram.exceptions import TelegramRetryAfter

import config as _config
from claude_session import (
    is_context_limit as _is_context_limit,
    usage_limit_reset as _session_limit_reset,
)
from config import MAX_RETRIES, STRINGS, TG_MSG_LIMIT, logger, t as _t_cfg
from telegram_io import (
    _send_safe,
    split_msg,
    typing_loop,
)
from tool_status import ToolStatusTracker

CHAT_EDIT_INTERVAL = 3.1  # keep all live bubbles below Telegram's ~20 edits/min ceiling
STREAM_FLOOD_SEND_INTERVAL = 3.0

_bot = None
_registry = None


def _retry_after(error: Exception) -> Optional[int]:
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        return int(retry_after)
    err = str(error)
    if "Flood control" not in err and "retry after" not in err.lower():
        return None
    match = re.search(r"retry after (\d+)", err, re.IGNORECASE)
    return int(match.group(1)) if match else 30


class _ChatEditBudgetBot:
    def __init__(self, bot):
        self._bot = bot
        self._lock = asyncio.Lock()
        self._next_edit_at = 0.0
        self.flood_until = 0.0

    def edit_ready(self, now: float) -> bool:
        return (
            not self._lock.locked()
            and now >= self._next_edit_at
            and now >= self.flood_until
        )

    async def send_message(self, *args, **kwargs):
        return await self._bot.send_message(*args, **kwargs)

    async def delete_message(self, *args, **kwargs):
        return await self._bot.delete_message(*args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        async with self._lock:
            now = monotonic()
            if now < self.flood_until:
                wait = max(1, math.ceil(self.flood_until - now))
                raise RuntimeError(f"Flood control active, retry after {wait}")
            delay = self._next_edit_at - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = monotonic()
            self._next_edit_at = now + CHAT_EDIT_INTERVAL
            try:
                return await self._bot.edit_message_text(*args, **kwargs)
            except Exception as e:
                wait = _retry_after(e)
                if wait is not None:
                    self.flood_until = now + wait + 1
                raise


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
    edit_bot = _ChatEditBudgetBot(_bot)

    parts: list[str] = []
    has_deltas = False
    current_msg_id: Optional[int] = None  # ID of the live message being edited
    last_edit_time = 0.0
    last_edit_text = ""
    finalized: list[int] = []
    terminal_handled = False

    status: Optional[ToolStatusTracker] = None

    async def _answer(text: str, **kwargs):
        if message is not None:
            return await message.answer(text, **kwargs)
        return await _bot.send_message(cid, text, **kwargs)

    async def _replace_live_message(text: str, now: float, **kwargs) -> bool:
        nonlocal current_msg_id, last_edit_time, last_edit_text
        old_msg_id = current_msg_id
        visible_text = text[:TG_MSG_LIMIT]
        last_edit_time = now
        try:
            m = await _answer(visible_text, **kwargs)
        except Exception as e:
            logger.debug(f"Flood fallback send failed: {e}")
            return False
        if not m:
            return False
        current_msg_id = m.message_id
        last_edit_text = visible_text
        if old_msg_id is not None and old_msg_id != current_msg_id:
            try:
                await _bot.delete_message(cid, old_msg_id)
            except Exception as e:
                logger.debug(f"Flood fallback cleanup failed: {e}")
        return True

    async def _edit_update():
        nonlocal current_msg_id, last_edit_time, last_edit_text
        text = "".join(parts)
        if not text:
            return
        visible_text = text[:TG_MSG_LIMIT]
        now = monotonic()

        # First chunk — send immediately without throttle
        if current_msg_id is None:
            try:
                m = await _answer(visible_text, parse_mode=None)
                if m:
                    current_msg_id = m.message_id
                    last_edit_text = visible_text
                    last_edit_time = now
            except Exception as e:
                logger.debug(f"Edit stream: initial send failed: {e}")
            return

        # Subsequent edits — apply throttle and flood control
        if now < edit_bot.flood_until:
            if (
                (now - last_edit_time) >= STREAM_FLOOD_SEND_INTERVAL
                and visible_text != last_edit_text
            ):
                await _replace_live_message(text, now, parse_mode=None)
            return
        if not edit_bot.edit_ready(now):
            return
        if visible_text == last_edit_text:
            return

        try:
            await edit_bot.edit_message_text(
                visible_text, chat_id=cid, message_id=current_msg_id, parse_mode=None
            )
            last_edit_text = visible_text
            last_edit_time = now
        except Exception as e:
            wait_sec = _retry_after(e)
            if wait_sec is not None:
                logger.info(f"Edit flood control, using send fallback for {wait_sec}s")
                await _replace_live_message(text, now, parse_mode=None)
            elif "message is not modified" in str(e):
                last_edit_text = visible_text
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
                if chunk_text == last_edit_text and not ent_dicts:
                    finalized.append(current_msg_id)
                    continue
                if monotonic() < edit_bot.flood_until:
                    if await _replace_live_message(
                        chunk_text,
                        monotonic(),
                        parse_mode=None,
                        entities=ent_dicts,
                    ):
                        finalized.append(current_msg_id)
                        continue
                # Final edit of the live message
                for attempt in range(3):
                    try:
                        await edit_bot.edit_message_text(
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

    async def _handle_usage_limit(err: str) -> None:
        nonlocal parts, has_deltas, current_msg_id, last_edit_time
        nonlocal last_edit_text, terminal_handled

        reset = _session_limit_reset(err) or ""
        notice = (
            _t_cfg(message, "session_limit", reset=reset)
            if message is not None
            else STRINGS["ru"]["session_limit"].format(reset=reset)
        )
        await _finalize_status()
        parts.clear()
        has_deltas = False

        delivered = False
        if current_msg_id is not None:
            try:
                await edit_bot.edit_message_text(
                    notice, chat_id=cid, message_id=current_msg_id, parse_mode=None
                )
                finalized.append(current_msg_id)
                delivered = True
            except Exception as edit_error:
                logger.warning(f"Chat {cid}: limit terminal edit failed: {edit_error}")
                try:
                    await _bot.delete_message(cid, current_msg_id)
                except Exception:
                    pass

        if not delivered:
            if message is not None:
                sent = await _send_safe(message, notice)
            else:
                sent = await _bot.send_message(cid, notice, parse_mode=None)
            if sent:
                finalized.append(sent.message_id)

        current_msg_id = None
        last_edit_time = 0.0
        last_edit_text = ""
        terminal_handled = True

    async def _handle_context_limit(
        key: str = "context_limit",
        *,
        latch: bool = True,
    ) -> None:
        nonlocal parts, has_deltas, current_msg_id, last_edit_time
        nonlocal last_edit_text, terminal_handled

        if latch:
            await _registry.get(cid).mark_context_reserve_blocked()
        notice = (
            _t_cfg(message, key)
            if message is not None
            else STRINGS["ru"][key]
        )
        await _finalize_status()
        parts.clear()
        has_deltas = False

        delivered = False
        if current_msg_id is not None:
            try:
                await edit_bot.edit_message_text(
                    notice, chat_id=cid, message_id=current_msg_id, parse_mode=None
                )
                finalized.append(current_msg_id)
                delivered = True
            except Exception as edit_error:
                logger.warning(
                    f"Chat {cid}: context terminal edit failed: {edit_error}"
                )
                try:
                    await _bot.delete_message(cid, current_msg_id)
                except Exception:
                    pass

        if not delivered:
            if message is not None:
                sent = await _send_safe(message, notice)
            else:
                sent = await _bot.send_message(cid, notice, parse_mode=None)
            if sent:
                finalized.append(sent.message_id)

        current_msg_id = None
        last_edit_time = 0.0
        last_edit_text = ""
        terminal_handled = True

    CHUNK_TIMEOUT_TEXT = 120
    CHUNK_TIMEOUT_TOOL = 300

    while retries <= MAX_RETRIES:
        need_retry = False
        _last_chunk_type = None
        logger.info(f"Chat {cid}: retry loop iteration retries={retries}/{MAX_RETRIES}")
        if retries:
            reserve = await _get_session(cid).check_context_reserve(prompt)
            if not reserve.get("ok"):
                reason = reserve.get("reason")
                key = (
                    "context_reserve"
                    if reason == "reserve"
                    else "session_unavailable"
                    if reason == "session_unavailable"
                    else "context_unknown"
                )
                logger.warning(
                    "Chat %s: retry rejected before query (%s)",
                    cid,
                    reason,
                )
                await _handle_context_limit(
                    key,
                    latch=reason == "reserve",
                )
                break
            if message is not None:
                await _send_safe(
                    message,
                    _t_cfg(message, "reconnecting", n=retries),
                )
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
                        status = ToolStatusTracker(edit_bot, message, cid)
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
                    reset = _session_limit_reset(err)
                    if chunk.get("kind") == "usage_limit" or reset is not None:
                        # лимит сессии — НЕ ретраить (reconnect бесполезен), сообщить и выйти
                        logger.warning(f"Chat {cid}: session limit hit, NOT retrying: {err}")
                        await _handle_usage_limit(err)
                        break
                    if chunk.get("kind") == "context_limit" or _is_context_limit(err):
                        logger.warning(
                            f"Chat {cid}: context full, NOT retrying automatic request"
                        )
                        await _handle_context_limit()
                        break
                    if chunk.get("kind") == "session_unavailable":
                        logger.warning(
                            f"Chat {cid}: saved session unavailable, NOT retrying"
                        )
                        await _handle_context_limit(
                            "session_unavailable",
                            latch=False,
                        )
                        break
                    if "session" in err.lower() or "process" in err.lower():
                        logger.warning(f"Session error, reconnecting: {err}")
                        _get_session(cid).reconnect()
                        retries += 1
                        if retries <= MAX_RETRIES and not finalized:
                            need_retry = True
                            try:
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
            # session-limit может прилететь исключением (не yield) → тоже НЕ ретраить
            reset = _session_limit_reset(str(e))
            if reset is not None:
                logger.warning(f"Chat {cid}: session limit (exception), NOT retrying: {e}")
                await _handle_usage_limit(str(e))
                break
            if _is_context_limit(str(e)):
                logger.warning(
                    f"Chat {cid}: context full (exception), NOT retrying"
                )
                await _handle_context_limit()
                break
            logger.error(f"Chat {cid}: outer exception in retry loop (retries={retries}): {type(e).__name__}: {e}", exc_info=True)
            retries += 1
            if retries <= MAX_RETRIES:
                _get_session(cid).reconnect()
                current_msg_id = None
                last_edit_text = ""
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

    if not text and not finalized and not terminal_handled:
        await _answer(STRINGS["ru"]["empty"] if message is None else _t_cfg(message, "empty"))
