"""Telegram I/O utilities: message helpers, typing loop, split, send_safe, draft helpers."""

import asyncio
import logging

from aiogram import types
from aiogram.enums import ChatAction
from aiogram.methods import SendMessageDraft

from config import TG_MSG_LIMIT, logger

_bot = None


def set_bot(bot_instance) -> None:
    global _bot
    _bot = bot_instance


def user_prefix(msg: types.Message) -> str:
    u = msg.from_user
    parts = []
    if u.first_name:
        parts.append(u.first_name)
    if u.last_name:
        parts.append(u.last_name)
    name = " ".join(parts) or "User"
    handle = f" (@{u.username})" if u.username else ""
    return f"[{name}{handle}]"


def extract_text_with_urls(msg: types.Message) -> str:
    """Extract message text with TEXT_LINK URLs inlined."""
    text = msg.text or msg.caption or ""
    if not text or not msg.entities:
        return text
    links = []
    for e in msg.entities:
        if e.type == "text_link" and e.url:
            anchor = text[e.offset:e.offset + e.length]
            links.append((e.offset, e.length, anchor, e.url))
    if not links:
        return text
    result = []
    prev = 0
    for offset, length, anchor, url in sorted(links):
        result.append(text[prev:offset])
        result.append(f"{anchor} ({url})")
        prev = offset + length
    result.append(text[prev:])
    return "".join(result)


def extract_caption_with_urls(msg: types.Message) -> str:
    """Extract caption text with TEXT_LINK URLs inlined."""
    text = msg.caption or ""
    if not text or not msg.caption_entities:
        return text
    links = []
    for e in msg.caption_entities:
        if e.type == "text_link" and e.url:
            anchor = text[e.offset:e.offset + e.length]
            links.append((e.offset, e.length, anchor, e.url))
    if not links:
        return text
    result = []
    prev = 0
    for offset, length, anchor, url in sorted(links):
        result.append(text[prev:offset])
        result.append(f"{anchor} ({url})")
        prev = offset + length
    result.append(text[prev:])
    return "".join(result)


def forward_meta(msg: types.Message) -> str:
    if not msg.forward_date:
        return ""
    fwd = "Forwarded"
    if msg.forward_from:
        name = msg.forward_from.first_name
        if msg.forward_from.last_name:
            name += " " + msg.forward_from.last_name
        fwd += f" from {name}"
    elif msg.forward_sender_name:
        fwd += f" from {msg.forward_sender_name}"
    return f"[{fwd}] "


def reply_meta(msg: types.Message) -> str:
    r = msg.reply_to_message
    if not r:
        return ""
    text = r.text or r.caption or ""
    if len(text) > 200:
        text = text[:200] + "..."
    return f"[reply: \"{text}\"]\n"


async def typing_loop(chat_id: int):
    while True:
        try:
            await _bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break


def split_msg(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def _send_safe(message: types.Message, text: str):
    from aiogram.exceptions import TelegramRetryAfter
    for attempt in range(3):
        try:
            return await message.answer(text)
        except TelegramRetryAfter as e:
            logger.warning(f"Flood control, retry after {e.retry_after}s (attempt {attempt+1})")
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            err_str = str(e)
            if "can't parse" in err_str.lower() or "parse entities" in err_str.lower():
                try:
                    return await message.answer(text, parse_mode=None)
                except TelegramRetryAfter as e2:
                    logger.warning(f"Flood control (plain), retry after {e2.retry_after}s")
                    await asyncio.sleep(e2.retry_after + 1)
                except Exception as e3:
                    logger.error(f"_send_safe plain fallback failed: {e3}")
                    return None
            else:
                logger.error(f"_send_safe unexpected error: {e}")
                try:
                    return await message.answer(text, parse_mode=None)
                except Exception:
                    return None
    return None


_draft_counter = 0


def _next_draft_id() -> int:
    global _draft_counter
    _draft_counter += 1
    return _draft_counter


async def _clear_draft(chat_id: int, did: int):
    try:
        await _bot(SendMessageDraft(chat_id=chat_id, draft_id=did, text=""))
    except Exception:
        pass
