"""All @dp.message handlers, command lists, set_commands(). Call register(dp) to attach."""

import asyncio
import logging
import os

from aiogram import Dispatcher, F, types
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram_media_group import media_group_handler

from chat_state import PendingEntry
import config as _cfg
from config import (
    ALLOWED,
    ALLOWED_MODELS,
    LOG_DIR,
    STRINGS,
    WORK_DIR,
    logger,
    t,
)
from media import (
    _media_name,
    download_file,
    log_size,
    media_count,
    transcribe,
)
from telegram_io import (
    _send_safe,
    extract_caption_with_urls,
    extract_text_with_urls,
    forward_meta,
    reply_meta,
    user_prefix,
)

_bot = None
_registry = None
_uptime_fn = None
_denied_notified: set[int] = set()


def set_bot(bot_instance) -> None:
    global _bot
    _bot = bot_instance


def set_registry(registry_instance) -> None:
    global _registry
    _registry = registry_instance


def set_uptime_fn(fn) -> None:
    global _uptime_fn
    _uptime_fn = fn


def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


async def _deny_once(msg: types.Message):
    uid = msg.from_user.id
    if uid not in _denied_notified:
        _denied_notified.add(uid)
        await _send_safe(msg, t(msg, "no_access", uid=uid))


async def enqueue(msg: types.Message, prompt: str):
    """Enqueue a user message into the ChatState pipeline."""
    chat_id = msg.chat.id
    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}{prompt}"

    mg = msg.media_group_id
    _kind = (
        "voice" if msg.voice else
        "video_note" if msg.video_note else
        "photo" if msg.photo else
        "video" if msg.video else
        "audio" if msg.audio else
        "document" if msg.document else
        "sticker" if msg.sticker else
        "text"
    )
    _preview = (msg.text or msg.caption or "")[:80].replace("\n", " ")
    logger.info(
        f"Chat {chat_id}: received msg_id={msg.message_id} from={msg.from_user.id} "
        f"kind={_kind} len={len(full_prompt)} mg={mg} preview={_preview!r}"
    )
    if _cfg.DEBUG:
        logger.debug(f"Chat {chat_id} raw prompt: {full_prompt}")

    entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
    )
    await _registry.get(chat_id).accept_entry(entry)


# --- Command handlers ---

async def h_start(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    s = _registry.get(msg.chat.id).session
    sid = s.session_id
    await _send_safe(msg, t(msg, "start",
        session=sid[:8] + "..." if sid else "new",
        model=s.model,
        cwd=WORK_DIR,
        debounce=_registry.get(msg.chat.id).debounce_sec,
        debug="on" if _cfg.DEBUG else "off",
    ))


async def h_help(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    await _send_safe(msg, t(msg, "help"))


async def h_status(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cs = _registry.get(msg.chat.id)
    s = cs.session
    sid = s.session_id
    rl = s.rate_limit
    if rl:
        util = rl.get('utilization')
        util_str = f" {int(util*100)}%" if util is not None else ""
        rl_str = f"{rl.get('status', '?')} ({rl.get('type', '?')}){util_str}"
    else:
        rl_str = "n/a"
    ctx = await s.get_context_usage()
    if ctx:
        ctx_str = f"{ctx['percentage']:.0f}% ({ctx['totalTokens']}/{ctx['maxTokens']})"
    else:
        ctx_str = "n/a"
    uptime = _uptime_fn() if _uptime_fn else "unknown"
    await _send_safe(msg, t(msg, "status",
        model=s.model,
        session=sid[:8] + "..." if sid else "none",
        cwd=WORK_DIR,
        debounce=cs.debounce_sec,
        debug="on" if _cfg.DEBUG else "off",
        uptime=uptime,
        context=ctx_str,
        rate_limit=rl_str,
        cost=f"{s.total_cost_usd:.4f}",
        media_count=media_count(),
        log_size=log_size(),
    ))


async def h_clear(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    cleared = await _registry.get(cid).request_clear()
    if not cleared:
        await _send_safe(msg, t(msg, "clear_busy"))
        return
    await _send_safe(msg, t(msg, "cleared"))


async def h_compact(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    cs = _registry.get(cid)
    if cs.is_busy:
        await cs.request_compact()
        await _send_safe(msg, "⏳ Сейчас идёт обработка, сжатие запланировано после.")
        return
    await cs.request_compact()


async def h_ping(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    sid = _registry.get(msg.chat.id).session.session_id
    await _send_safe(msg, t(msg, "ping", session=sid or "none"))


async def h_model(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) > 1:
        name = args[1].strip().lower()
        use_200k = "200k" in name
        name_clean = name.replace("200k", "").replace("1m", "").strip()
        model_id = ALLOWED_MODELS.get(name_clean)
        if not model_id:
            await _send_safe(msg, "Unknown model. Available: opus, sonnet, haiku (+ 200k)")
            return
        use_1m = not use_200k
        await _registry.get(msg.chat.id).set_model(model_id, use_1m)
        ctx = "200K" if use_200k else "1M"
        await _send_safe(msg, t(msg, "model_set", model=f"{model_id} ({ctx})"))
    else:
        await _send_safe(msg, t(msg, "model_usage", model=_registry.get(msg.chat.id).session.model))


async def h_debounce(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) > 1:
        try:
            val = int(args[1].strip())
            if 0 <= val <= 30:
                await _registry.get(msg.chat.id).set_debounce(val)
                await _send_safe(msg, t(msg, "debounce_set", sec=val))
            else:
                await _send_safe(msg, "0-30 sec")
        except ValueError:
            await _send_safe(msg, t(msg, "debounce_usage", sec=_registry.get(msg.chat.id).debounce_sec))
    else:
        await _send_safe(msg, t(msg, "debounce_usage", sec=_registry.get(msg.chat.id).debounce_sec))


async def h_debug(msg: types.Message):
    import config as _cfg
    import logging as _logging
    if not allowed(msg.from_user.id):
        return
    _cfg.DEBUG = not _cfg.DEBUG
    logger.setLevel(_logging.DEBUG if _cfg.DEBUG else _logging.INFO)
    if _cfg.DEBUG:
        await _send_safe(msg, t(msg, "debug_on", path=str(LOG_DIR / "kesha.log")))
    else:
        await _send_safe(msg, t(msg, "debug_off"))


async def h_restart(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    await _send_safe(msg, t(msg, "restarting"))
    p = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "kesha-bot",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await p.communicate()


async def h_stop(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    stopped = await _registry.get(cid).request_stop()
    if stopped:
        await _send_safe(msg, "Stopping...")
    else:
        await _send_safe(msg, "Nothing to stop.")


# --- Media handlers ---

async def h_voice(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    cs = _registry.get(chat_id)
    path = await download_file(msg.voice.file_id, _media_name("voice", ".oga", msg), msg.voice.file_unique_id)
    if not path:
        await enqueue(msg, "[voice: файл слишком большой]")
        return
    from config import DEEPGRAM as _DG
    if not _DG:
        await enqueue(msg, f"[voice: {path}]")
        return

    gen, media_gen = await cs.transcription_started()
    await _bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        text, err = await transcribe(path, msg.voice.file_unique_id or "")
    except Exception as e:
        text, err = "", str(e)

    if not text:
        await cs.transcription_finished(None, gen, media_gen)
        err_msg = t(msg, "voice_fail")
        if err:
            err_msg += f" ({err})"
        await _send_safe(msg, err_msg)
        return

    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[voice: {path} | {text}]"
    entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
    )
    await cs.transcription_finished(entry, gen, media_gen)


@media_group_handler
async def h_media_album(messages: list[types.Message]):
    if not allowed(messages[0].from_user.id):
        return await _deny_once(messages[0])
    parts = []
    for m in messages:
        if m.photo:
            path = await download_file(m.photo[-1].file_id, _media_name("photo", ".jpg", m), m.photo[-1].file_unique_id)
            tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
            parts.append(tag)
        elif m.video:
            path = await download_file(m.video.file_id, m.video.file_name or _media_name("video", ".mp4", m), m.video.file_unique_id)
            tag = f"[video: {path}]" if path else "[video: файл слишком большой]"
            parts.append(tag)
        elif m.document:
            doc = m.document
            ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
            path = await download_file(doc.file_id, doc.file_name or _media_name("doc", ext, m), doc.file_unique_id)
            tag = f"[document: {path} ({doc.file_name})]" if path else f"[document: файл слишком большой ({doc.file_name})]"
            parts.append(tag)
        elif m.audio:
            name = m.audio.file_name or _media_name("audio", ".mp3", m)
            path = await download_file(m.audio.file_id, name, m.audio.file_unique_id)
            tag = f"[audio: {path} ({name})]" if path else f"[audio: файл слишком большой ({name})]"
            parts.append(tag)
    caption = ""
    for m in messages:
        if m.caption:
            caption = f"\n{extract_caption_with_urls(m)}"
            break
    fwd = forward_meta(messages[0])
    media_block = "\n".join(parts)
    await enqueue(messages[0], f"{fwd}{media_block}{caption}")


async def h_photo(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.photo[-1].file_id, _media_name("photo", ".jpg", msg), msg.photo[-1].file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}")


async def h_video_note(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    cs = _registry.get(chat_id)
    path = await download_file(msg.video_note.file_id, _media_name("videonote", ".mp4", msg), msg.video_note.file_unique_id)
    if not path:
        await enqueue(msg, "[video_note: файл слишком большой]")
        return
    from config import DEEPGRAM as _DG
    if not _DG:
        await enqueue(msg, f"[video_note: {path}]")
        return

    gen, media_gen = await cs.transcription_started()
    audio_path = path.replace(".mp4", ".oga")
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", path, "-vn", "-acodec", "libopus", "-y", audio_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await p.communicate()
    if p.returncode == 0:
        await _bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            text, err = await transcribe(audio_path, msg.video_note.file_unique_id or "")
        except Exception as e:
            text, err = "", str(e)
        if text:
            full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[video_note: {path} | {text}]"
            entry = PendingEntry(
                prompt=full_prompt,
                message_id=msg.message_id,
                message=msg,
                source="user",
                reply_target=chat_id,
            )
            await cs.transcription_finished(entry, gen, media_gen)
            return
    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[video_note: {path}]"
    fallback_entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
    )
    await cs.transcription_finished(fallback_entry, gen, media_gen)


async def h_document(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    doc = msg.document
    ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
    path = await download_file(doc.file_id, doc.file_name or _media_name("doc", ext, msg), doc.file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[document: {path} ({doc.file_name})]" if path else f"[document: файл слишком большой ({doc.file_name})]"
    await enqueue(msg, f"{tag}{caption}")


async def h_sticker(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    emoji = msg.sticker.emoji or "?"
    await enqueue(msg, f"[sticker: {emoji}]")


async def h_video(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.video.file_id, msg.video.file_name or _media_name("video", ".mp4", msg), msg.video.file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[video: {path}]" if path else "[video: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}")


async def h_audio(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    ext = os.path.splitext(msg.audio.file_name or "audio.mp3")[1] or ".mp3"
    name = msg.audio.file_name or _media_name("audio", ext, msg)
    path = await download_file(msg.audio.file_id, name, msg.audio.file_unique_id)
    tag = f"[audio: {path} ({name})]" if path else f"[audio: файл слишком большой ({name})]"
    await enqueue(msg, tag)


async def h_text(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    await enqueue(msg, extract_text_with_urls(msg))


async def h_fallback(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    text = msg.text or msg.caption or ""
    content_type = msg.content_type or "unknown"
    logger.warning(f"Chat {msg.chat.id}: unhandled message type={content_type}, text={text[:100]}")
    if text:
        await enqueue(msg, text)


# --- Command lists ---

COMMANDS_RU = [
    BotCommand(command="start", description="Статус бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="status", description="Подробный статус"),
    BotCommand(command="clear", description="Сбросить сессию"),
    BotCommand(command="compact", description="Сжать контекст (сохранить краткую выжимку)"),
    BotCommand(command="ping", description="Проверить сессию"),
    BotCommand(command="model", description="Сменить модель"),
    BotCommand(command="debounce", description="Задержка склейки сообщений"),
    BotCommand(command="debug", description="Вкл/выкл debug логи"),
    BotCommand(command="restart", description="Перезапустить бота"),
]

COMMANDS_EN = [
    BotCommand(command="start", description="Bot status"),
    BotCommand(command="help", description="Command reference"),
    BotCommand(command="status", description="Detailed status"),
    BotCommand(command="clear", description="Clear session"),
    BotCommand(command="compact", description="Compact context (keep a summary)"),
    BotCommand(command="ping", description="Check session"),
    BotCommand(command="model", description="Change model"),
    BotCommand(command="debounce", description="Message batching delay"),
    BotCommand(command="debug", description="Toggle debug logs"),
    BotCommand(command="restart", description="Restart bot"),
]


async def set_commands(bot):
    from aiogram.types import (
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeChat,
    )
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    ):
        for lang in (None, "ru", "en"):
            try:
                if lang:
                    await bot.delete_my_commands(scope=scope, language_code=lang)
                else:
                    await bot.delete_my_commands(scope=scope)
            except Exception as e:
                logger.debug(f"delete_my_commands({type(scope).__name__}, {lang}) failed: {e}")

    for uid in ALLOWED:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid), language_code="ru")
        except Exception as e:
            logger.debug(f"delete_my_commands chat={uid} failed: {e}")

    await bot.set_my_commands(COMMANDS_EN, scope=BotCommandScopeDefault())
    await bot.set_my_commands(COMMANDS_RU, scope=BotCommandScopeDefault(), language_code="ru")
    logger.info("Bot commands set (RU + EN), purged other scopes")


def register(dp: Dispatcher) -> None:
    """Attach all handlers to dp. Preserves exact registration order."""
    dp.message.register(h_start, CommandStart())
    dp.message.register(h_help, Command("help"))
    dp.message.register(h_status, Command("status"))
    dp.message.register(h_clear, Command("clear"))
    dp.message.register(h_compact, Command("compact"))
    dp.message.register(h_ping, Command("ping"))
    dp.message.register(h_model, Command("model"))
    dp.message.register(h_debounce, Command("debounce"))
    dp.message.register(h_debug, Command("debug"))
    dp.message.register(h_restart, Command("restart"))
    dp.message.register(h_stop, Command("stop"))
    # Media handlers — media_group BEFORE photo
    dp.message.register(h_voice, F.voice)
    dp.message.register(h_media_album, F.media_group_id)
    dp.message.register(h_photo, F.photo)
    dp.message.register(h_video_note, F.video_note)
    dp.message.register(h_document, F.document)
    dp.message.register(h_sticker, F.sticker)
    dp.message.register(h_video, F.video)
    dp.message.register(h_audio, F.audio)
    # text BEFORE fallback
    dp.message.register(h_text, F.text)
    dp.message.register(h_fallback)
