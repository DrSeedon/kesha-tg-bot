"""Kesha Telegram Bot — Claude Agent SDK with persistent sessions."""

import asyncio
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import BotCommand, BotCommandScopeDefault
from dotenv import load_dotenv

from claude_session import ClaudeSession

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}
WORK_DIR = os.getenv("WORK_DIR", ".")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
DEEPGRAM = os.getenv("DEEPGRAM_API_KEY", "")
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
MAX_RETRIES = 2
DEBOUNCE_SEC = int(os.getenv("DEBOUNCE_SEC", "3"))
TG_MSG_LIMIT = 4096
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./storage/media"))
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs"))
MEDIA_MAX_AGE_H = 24

# --- Logging ---

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("kesha")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

_fh = RotatingFileHandler(LOG_DIR / "kesha.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

# --- i18n ---

STRINGS = {
    "ru": {
        "no_access": "Нет доступа.",
        "start": (
            "🦜 *Кеша на связи!*\n"
            "📌 Session: `{session}`\n"
            "🤖 Model: `{model}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`"
        ),
        "cleared": "🧹 Сессия сброшена. Новая начнётся с следующего сообщения.",
        "ping": "🏓 Session: `{session}`",
        "model_set": "✅ Модель: `{model}`",
        "model_usage": "Текущая: `{model}`\nИспользование: `/model claude-sonnet-4-6`",
        "debug_on": "🐛 Debug включён. Логи: `{path}`",
        "debug_off": "🐛 Debug выключен.",
        "debounce_set": "⏱ Debounce: `{sec}s`",
        "debounce_usage": "Текущий: `{sec}s`\nИспользование: `/debounce 5`",
        "reconnecting": "⚠️ Переподключаюсь... (попытка {n})",
        "error_retry": "⚠️ Ошибка, перезапуск сессии (попытка {n})...",
        "empty": "🤷 Пустой ответ",
        "voice_fail": "🎙️ Не удалось расшифровать голосовое.",
        "deepgram_error": "🎙️ Ошибка транскрипции: {err}",
        "help": (
            "🦜 *Kesha Bot — команды:*\n\n"
            "/start — статус бота\n"
            "/help — эта справка\n"
            "/clear — сбросить сессию\n"
            "/ping — проверить сессию\n"
            "/model `<name>` — сменить модель\n"
            "/debounce `<sec>` — задержка склейки сообщений\n"
            "/debug — вкл/выкл debug логирование\n\n"
            "📎 Поддерживаю: текст, фото, голосовые, видео, документы, аудио, видеокружки, стикеры, пересланные сообщения."
        ),
        "status": (
            "📊 *Статус Kesha:*\n\n"
            "🤖 Model: `{model}`\n"
            "📌 Session: `{session}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`\n"
            "📁 Media: `{media_count}` файлов\n"
            "📝 Log: `{log_size}`"
        ),
    },
    "en": {
        "no_access": "No access.",
        "start": (
            "🦜 *Kesha online!*\n"
            "📌 Session: `{session}`\n"
            "🤖 Model: `{model}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`"
        ),
        "cleared": "🧹 Session cleared. New one starts with next message.",
        "ping": "🏓 Session: `{session}`",
        "model_set": "✅ Model: `{model}`",
        "model_usage": "Current: `{model}`\nUsage: `/model claude-sonnet-4-6`",
        "debug_on": "🐛 Debug enabled. Logs: `{path}`",
        "debug_off": "🐛 Debug disabled.",
        "debounce_set": "⏱ Debounce: `{sec}s`",
        "debounce_usage": "Current: `{sec}s`\nUsage: `/debounce 5`",
        "reconnecting": "⚠️ Reconnecting... (attempt {n})",
        "error_retry": "⚠️ Error, restarting session (attempt {n})...",
        "empty": "🤷 Empty response",
        "voice_fail": "🎙️ Could not transcribe voice message.",
        "deepgram_error": "🎙️ Transcription error: {err}",
        "help": (
            "🦜 *Kesha Bot — commands:*\n\n"
            "/start — bot status\n"
            "/help — this help\n"
            "/clear — reset session\n"
            "/ping — check session\n"
            "/model `<name>` — change model\n"
            "/debounce `<sec>` — message batching delay\n"
            "/debug — toggle debug logging\n\n"
            "📎 Supports: text, photos, voice, video, documents, audio, video notes, stickers, forwarded messages."
        ),
        "status": (
            "📊 *Kesha Status:*\n\n"
            "🤖 Model: `{model}`\n"
            "📌 Session: `{session}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`\n"
            "📁 Media: `{media_count}` files\n"
            "📝 Log: `{log_size}`"
        ),
    },
}


def t(msg: types.Message, key: str, **kw) -> str:
    lang = (msg.from_user.language_code or "en")[:2]
    if lang not in STRINGS:
        lang = "en"
    return STRINGS[lang][key].format(**kw)


# --- System Prompt ---

SYSTEM_PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"


def load_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        raw = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
        return raw.format(cwd=WORK_DIR, media_dir=MEDIA_DIR)
    return ""


# --- Media ---

MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_media():
    cutoff = time.time() - MEDIA_MAX_AGE_H * 3600
    count = 0
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old media files")


def media_count() -> int:
    return sum(1 for f in MEDIA_DIR.iterdir() if f.is_file())


def log_size() -> str:
    p = LOG_DIR / "kesha.log"
    if not p.exists():
        return "0 KB"
    size = p.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size // 1024} KB"
    return f"{size // (1024 * 1024)} MB"


# --- Bot ---

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
claude = ClaudeSession(
    cwd=WORK_DIR,
    model=MODEL,
    system_prompt=load_system_prompt(),
)


def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


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


async def typing_loop(chat_id: int):
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break


async def transcribe(path: str) -> tuple[str, str | None]:
    p = await asyncio.create_subprocess_exec(
        "curl", "-s", "--request", "POST",
        "--url", "https://api.deepgram.com/v1/listen?model=nova-2&language=ru&smart_format=true",
        "--header", f"Authorization: Token {DEEPGRAM}",
        "--header", "Content-Type: audio/ogg",
        "--data-binary", f"@{path}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await p.communicate()
    try:
        data = json.loads(out)
        if "error" in data:
            return "", data["error"]
        if "err_msg" in data:
            return "", data["err_msg"]
        text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        return text, None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raw = out.decode(errors="replace")[:200]
        logger.error(f"Deepgram parse error: {e}, raw: {raw}")
        return "", str(e)


async def download_file(file_id: str, ext: str, msg_id: int) -> str:
    f = await bot.get_file(file_id)
    path = MEDIA_DIR / f"kesha_{msg_id}{ext}"
    await bot.download_file(f.file_path, str(path))
    return str(path)


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


# --- Debounce + Queue ---

_pending: dict[int, list[dict]] = {}
_pending_timers: dict[int, asyncio.Task] = {}
_processing: set[int] = set()
_queue: dict[int, list[list[dict]]] = {}


def _make_entry(msg: types.Message, prompt: str) -> dict:
    return {"msg": msg, "prompt": prompt}


async def _debounce_fire(chat_id: int):
    await asyncio.sleep(DEBOUNCE_SEC)
    batch = _pending.pop(chat_id, [])
    _pending_timers.pop(chat_id, None)
    if not batch:
        return

    if chat_id in _processing:
        _queue.setdefault(chat_id, []).append(batch)
        logger.debug(f"Chat {chat_id}: queued batch ({len(batch)} msgs), processing busy")
        return

    await _process_batch(chat_id, batch)


async def _process_batch(chat_id: int, batch: list[dict]):
    _processing.add(chat_id)
    try:
        last_msg = batch[-1]["msg"]
        combined = "\n\n".join(e["prompt"] for e in batch)

        logger.info(f"Chat {chat_id}: sending batch ({len(batch)} msgs, {len(combined)} chars)")
        if DEBUG:
            logger.debug(f"Chat {chat_id} prompt: {combined}")

        await _ask(last_msg, combined)
    finally:
        _processing.discard(chat_id)
        queued = _queue.get(chat_id)
        if queued:
            next_batch = queued.pop(0)
            if not queued:
                del _queue[chat_id]
            asyncio.create_task(_process_batch(chat_id, next_batch))


async def enqueue(msg: types.Message, prompt: str):
    chat_id = msg.chat.id
    prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{prompt}"

    logger.info(f"Chat {chat_id}: received from {msg.from_user.id}")
    if DEBUG:
        logger.debug(f"Chat {chat_id} raw prompt: {prompt}")

    _pending.setdefault(chat_id, []).append(_make_entry(msg, prompt))

    old_timer = _pending_timers.get(chat_id)
    if old_timer and not old_timer.done():
        old_timer.cancel()

    _pending_timers[chat_id] = asyncio.create_task(_debounce_fire(chat_id))


async def _send_safe(message: types.Message, text: str):
    try:
        await message.answer(text)
    except Exception:
        await message.answer(text, parse_mode=None)


async def _ask(message: types.Message, prompt: str):
    cid = message.chat.id
    typer = asyncio.create_task(typing_loop(cid))
    parts = []
    retries = 0

    while retries <= MAX_RETRIES:
        try:
            async for chunk in claude.send_message(prompt):
                ct = chunk["type"]
                if ct in ("text", "text_delta"):
                    parts.append(chunk["content"])
                    if DEBUG:
                        logger.debug(f"Chat {cid} chunk: {chunk['content'][:100]}")
                elif ct == "tool":
                    tool_name = chunk.get("name", "?")
                    logger.info(f"Chat {cid} tool: {tool_name}")
                elif ct == "error":
                    err = chunk["content"]
                    if "session" in err.lower() or "process" in err.lower():
                        logger.warning(f"Session error, resetting: {err}")
                        claude.reset()
                        retries += 1
                        if retries <= MAX_RETRIES:
                            await _send_safe(message, t(message, "reconnecting", n=retries))
                            continue
                    parts.append(f"Error: {err}")
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            retries += 1
            if retries <= MAX_RETRIES:
                claude.reset()
                await _send_safe(message, t(message, "error_retry", n=retries))
            else:
                parts.append(f"Error: {e}")
                break

    typer.cancel()
    text = "".join(parts) or t(message, "empty")

    logger.info(f"Chat {cid}: response {len(text)} chars")
    if DEBUG:
        logger.debug(f"Chat {cid} full response: {text[:500]}")

    for p in split_msg(text):
        await _send_safe(message, p)


# --- Commands ---

COMMANDS_RU = [
    BotCommand(command="start", description="Статус бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="status", description="Подробный статус"),
    BotCommand(command="clear", description="Сбросить сессию"),
    BotCommand(command="ping", description="Проверить сессию"),
    BotCommand(command="model", description="Сменить модель"),
    BotCommand(command="debounce", description="Задержка склейки сообщений"),
    BotCommand(command="debug", description="Вкл/выкл debug логи"),
]

COMMANDS_EN = [
    BotCommand(command="start", description="Bot status"),
    BotCommand(command="help", description="Command reference"),
    BotCommand(command="status", description="Detailed status"),
    BotCommand(command="clear", description="Clear session"),
    BotCommand(command="ping", description="Check session"),
    BotCommand(command="model", description="Change model"),
    BotCommand(command="debounce", description="Message batching delay"),
    BotCommand(command="debug", description="Toggle debug logs"),
]


async def set_commands():
    await bot.set_my_commands(COMMANDS_EN, scope=BotCommandScopeDefault())
    await bot.set_my_commands(COMMANDS_RU, scope=BotCommandScopeDefault(), language_code="ru")
    logger.info("Bot commands set (RU + EN)")


# --- Handlers ---

@dp.message(CommandStart())
async def h_start(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _send_safe(msg, t(msg, "no_access"))
    sid = claude.session_id
    await _send_safe(msg, t(msg, "start",
        session=sid[:8] + "..." if sid else "new",
        model=claude.model,
        cwd=WORK_DIR,
        debounce=DEBOUNCE_SEC,
        debug="on" if DEBUG else "off",
    ))


@dp.message(Command("help"))
async def h_help(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    await _send_safe(msg, t(msg, "help"))


@dp.message(Command("status"))
async def h_status(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    sid = claude.session_id
    await _send_safe(msg, t(msg, "status",
        model=claude.model,
        session=sid[:8] + "..." if sid else "none",
        cwd=WORK_DIR,
        debounce=DEBOUNCE_SEC,
        debug="on" if DEBUG else "off",
        media_count=media_count(),
        log_size=log_size(),
    ))


@dp.message(Command("clear"))
async def h_clear(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    claude.reset()
    await _send_safe(msg, t(msg, "cleared"))


@dp.message(Command("ping"))
async def h_ping(msg: types.Message):
    await _send_safe(msg, t(msg, "ping", session=claude.session_id or "none"))


@dp.message(Command("model"))
async def h_model(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) > 1:
        claude.model = args[1].strip()
        await _send_safe(msg, t(msg, "model_set", model=claude.model))
    else:
        await _send_safe(msg, t(msg, "model_usage", model=claude.model))


@dp.message(Command("debounce"))
async def h_debounce(msg: types.Message):
    global DEBOUNCE_SEC
    if not allowed(msg.from_user.id):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) > 1:
        try:
            val = int(args[1].strip())
            if 0 <= val <= 30:
                DEBOUNCE_SEC = val
                await _send_safe(msg, t(msg, "debounce_set", sec=DEBOUNCE_SEC))
            else:
                await _send_safe(msg, "0-30 sec")
        except ValueError:
            await _send_safe(msg, t(msg, "debounce_usage", sec=DEBOUNCE_SEC))
    else:
        await _send_safe(msg, t(msg, "debounce_usage", sec=DEBOUNCE_SEC))


@dp.message(Command("debug"))
async def h_debug(msg: types.Message):
    global DEBUG
    if not allowed(msg.from_user.id):
        return
    DEBUG = not DEBUG
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    if DEBUG:
        await _send_safe(msg, t(msg, "debug_on", path=str(LOG_DIR / "kesha.log")))
    else:
        await _send_safe(msg, t(msg, "debug_off"))


@dp.message(F.voice)
async def h_voice(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.voice.file_id, ".oga", msg.message_id)
    await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    text, err = await transcribe(path)
    if not text:
        err_msg = t(msg, "voice_fail")
        if err:
            err_msg += f" ({err})"
        return await _send_safe(msg, err_msg)
    await enqueue(msg, f"[voice: {path} | {text}]")


@dp.message(F.photo)
async def h_photo(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.photo[-1].file_id, ".jpg", msg.message_id)
    caption = f"\n{msg.caption}" if msg.caption else ""
    await enqueue(msg, f"[photo: {path}]{caption}")


@dp.message(F.video_note)
async def h_video_note(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.video_note.file_id, ".mp4", msg.message_id)
    await enqueue(msg, f"[video_note: {path}]")


@dp.message(F.document)
async def h_document(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    doc = msg.document
    ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
    path = await download_file(doc.file_id, ext, msg.message_id)
    caption = f"\n{msg.caption}" if msg.caption else ""
    await enqueue(msg, f"[document: {path} ({doc.file_name})]{caption}")


@dp.message(F.sticker)
async def h_sticker(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    emoji = msg.sticker.emoji or "?"
    await enqueue(msg, f"[sticker: {emoji}]")


@dp.message(F.video)
async def h_video(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.video.file_id, ".mp4", msg.message_id)
    caption = f"\n{msg.caption}" if msg.caption else ""
    await enqueue(msg, f"[video: {path}]{caption}")


@dp.message(F.audio)
async def h_audio(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    ext = os.path.splitext(msg.audio.file_name or "audio.mp3")[1] or ".mp3"
    path = await download_file(msg.audio.file_id, ext, msg.message_id)
    name = msg.audio.file_name or "audio"
    await enqueue(msg, f"[audio: {path} ({name})]")


@dp.message(F.text)
async def h_text(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    await enqueue(msg, msg.text)


# --- Main ---

async def main():
    cleanup_media()
    await set_commands()
    logger.info(f"Kesha bot | CWD={WORK_DIR} | Model={MODEL} | Debug={DEBUG}")
    logger.info(f"Allowed: {ALLOWED or 'all'} | Media: {MEDIA_DIR} | Logs: {LOG_DIR}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
