"""Kesha Telegram Bot — Claude Agent SDK with persistent sessions."""

import asyncio
import json
import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram_media_group import media_group_handler
from aiogram.filters import CommandStart, Command
from aiogram.methods import SendMessageDraft
from aiogram.types import BotCommand, BotCommandScopeDefault
from dotenv import load_dotenv

from typing import Optional

from claude_session import ClaudeSession
from kesha_tools import kesha_server, set_bot_ref, set_current_chat
import reminders as _reminders
from tool_status import ToolStatusTracker
import compact as _compact
from chat_state import ChatRegistry, ChatPhase, PendingEntry

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}
WORK_DIR = os.getenv("WORK_DIR", ".")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
DEEPGRAM = os.getenv("DEEPGRAM_API_KEY", "")
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
MAX_RETRIES = 2
DEBOUNCE_SEC = int(os.getenv("DEBOUNCE_SEC", "3"))
AUTO_COMPACT_PCT = float(os.getenv("AUTO_COMPACT_PCT", "95"))
TG_MSG_LIMIT = 4096
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./storage/media")).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs")).resolve()
GREET_FLAG = Path(__file__).parent / "storage" / "greet_on_restart"
MEDIA_MAX_AGE_H = 24

# --- Logging ---

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("kesha")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

import time as _time_mod
_time_mod.tzset() if hasattr(_time_mod, 'tzset') else None  # ensure TZ is loaded
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
# Pin log timestamps to Krsk local time (UTC+7) regardless of service env
from datetime import datetime, timezone, timedelta
_KRSK = timezone(timedelta(hours=7))
def _krsk_time(record, datefmt=None):
    dt = datetime.fromtimestamp(record.created, tz=_KRSK)
    if datefmt:
        return dt.strftime(datefmt)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"
_fmt.formatTime = _krsk_time  # type: ignore[assignment]

# Only attach file handler when running as main (not during `python -c "import bot"` smoke tests)
if not logger.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)

    if __name__ == "__main__" or os.environ.get("KESHA_MAIN") == "1":
        # Daily rotation, keep 7 days (like media cleanup)
        _fh = TimedRotatingFileHandler(
            LOG_DIR / "kesha.log", when="midnight", interval=1,
            backupCount=7, encoding="utf-8", utc=False,
        )
        _fh.setFormatter(_fmt)
        logger.addHandler(_fh)

# --- i18n ---

STRINGS = {
    "ru": {
        "no_access": "🔒 Нет доступа.\n\nТвой Telegram ID: `{uid}`\nПопроси владельца добавить тебя.",
        "start": (
            "🦜 *Кеша на связи!*\n"
            "📌 Session: `{session}`\n"
            "🤖 Model: `{model}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`"
        ),
        "cleared": "🧹 Сессия сброшена. Новая начнётся с следующего сообщения.",
        "clear_busy": "⏳ Подожди, сейчас идёт обработка. Попробуй через секунду.",
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
        "restarting": "🔄 Перезапускаюсь...",
        "started": "🦜 Кеша запущен!",
        "help": (
            "🦜 *Kesha Bot — команды:*\n\n"
            "/start — статус бота\n"
            "/help — эта справка\n"
            "/clear — сбросить сессию\n"
            "/ping — проверить сессию\n"
            "/status — подробный статус\n"
            "/model `<name>` — сменить модель\n"
            "/debounce `<sec>` — задержка склейки сообщений\n"
            "/debug — вкл/выкл debug логирование\n"
            "/restart — перезапустить бота\n\n"
            "📎 Поддерживаю: текст, фото, голосовые, видео, документы, аудио, видеокружки, стикеры, пересланные сообщения."
        ),
        "status": (
            "📊 *Статус Kesha:*\n\n"
            "🤖 Модель: `{model}`\n"
            "📌 Сессия: `{session}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Дебаунс: `{debounce}s`\n"
            "🐛 Debug: `{debug}`\n"
            "⏳ Аптайм: `{uptime}`\n"
            "🧠 Контекст: `{context}`\n"
            "📊 Rate limit: `{rate_limit}`\n"
            "💰 Стоимость сессии: `${cost}`\n"
            "📁 Медиа: `{media_count}` файлов\n"
            "📝 Лог: `{log_size}`"
        ),
    },
    "en": {
        "no_access": "🔒 No access.\n\nYour Telegram ID: `{uid}`\nAsk the owner to add you.",
        "start": (
            "🦜 *Kesha online!*\n"
            "📌 Session: `{session}`\n"
            "🤖 Model: `{model}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`"
        ),
        "cleared": "🧹 Session cleared. New one starts with next message.",
        "clear_busy": "⏳ Processing in progress. Try again in a moment.",
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
        "restarting": "🔄 Restarting...",
        "started": "🦜 Kesha started!",
        "help": (
            "🦜 *Kesha Bot — commands:*\n\n"
            "/start — bot status\n"
            "/help — this help\n"
            "/clear — reset session\n"
            "/ping — check session\n"
            "/status — detailed status\n"
            "/model `<name>` — change model\n"
            "/debounce `<sec>` — message batching delay\n"
            "/debug — toggle debug logging\n"
            "/restart — restart bot\n\n"
            "📎 Supports: text, photos, voice, video, documents, audio, video notes, stickers, forwarded messages."
        ),
        "status": (
            "📊 *Kesha Status:*\n\n"
            "🤖 Model: `{model}`\n"
            "📌 Session: `{session}`\n"
            "📂 CWD: `{cwd}`\n"
            "⏱ Debounce: `{debounce}s`\n"
            "🐛 Debug: `{debug}`\n"
            "⏳ Uptime: `{uptime}`\n"
            "🧠 Context: `{context}`\n"
            "📊 Rate limit: `{rate_limit}`\n"
            "💰 Session cost: `${cost}`\n"
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
    global _file_cache
    cutoff = time.time() - MEDIA_MAX_AGE_H * 3600
    count = 0
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.name not in (".cache.json", ".transcription_cache.json") and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old media files")
        _file_cache = {k: v for k, v in _file_cache.items() if Path(v).exists()}
        _save_cache(_file_cache)


LOG_MAX_AGE_DAYS = 7


def cleanup_logs():
    """Delete log backup files older than LOG_MAX_AGE_DAYS. Active kesha.log is managed by TimedRotatingFileHandler."""
    cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
    count = 0
    for f in LOG_DIR.iterdir():
        # TimedRotatingFileHandler names backups like kesha.log.2026-04-13
        if f.is_file() and f.name.startswith("kesha.log.") and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old log files (>{LOG_MAX_AGE_DAYS}d)")


async def daily_cleanup_loop():
    """Run media + log cleanup every 24h while bot is alive."""
    while True:
        await asyncio.sleep(86400)  # 24h
        try:
            cleanup_media()
            cleanup_logs()
        except Exception as e:
            logger.error(f"Daily cleanup error: {e}")


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


import sys
set_bot_ref(sys.modules[__name__])


_denied_notified: set[int] = set()

def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED

async def _deny_once(msg: types.Message):
    uid = msg.from_user.id
    if uid not in _denied_notified:
        _denied_notified.add(uid)
        await _send_safe(msg, t(msg, "no_access", uid=uid))


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
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break


TRANSCRIPTION_CACHE_FILE = MEDIA_DIR / ".transcription_cache.json"
_transcription_cache: dict[str, str] = {}


def _load_transcription_cache() -> dict[str, str]:
    if TRANSCRIPTION_CACHE_FILE.exists():
        try:
            return json.loads(TRANSCRIPTION_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_transcription_cache():
    TRANSCRIPTION_CACHE_FILE.write_text(json.dumps(_transcription_cache, ensure_ascii=False))


_transcription_cache = _load_transcription_cache()


async def transcribe(path: str, unique_id: str = "") -> tuple[str, str | None]:
    if unique_id and unique_id in _transcription_cache:
        cached = _transcription_cache[unique_id]
        logger.info(f"Transcription cache hit: {unique_id} ({len(cached)} chars)")
        return cached, None

    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as _http:
            with open(path, "rb") as _af:
                audio_data = _af.read()
            async with _http.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=ru&smart_format=true",
                headers={"Authorization": f"Token {DEEPGRAM}", "Content-Type": "audio/ogg"},
                data=audio_data,
                timeout=_aiohttp.ClientTimeout(total=120),
            ) as resp:
                out = await resp.read()
    except Exception as e:
        logger.error(f"Deepgram request error: {e}")
        return "", str(e)
    try:
        data = json.loads(out)
        if "error" in data:
            return "", data["error"]
        if "err_msg" in data:
            return "", data["err_msg"]
        text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        duration = data.get("metadata", {}).get("duration", 0)
        cost = duration / 60 * 0.0043
        logger.info(f"Deepgram: {duration:.1f}s, ${cost:.4f}, {len(text)} chars")
        if unique_id and text:
            _transcription_cache[unique_id] = text
            _save_transcription_cache()
            logger.info(f"Transcription cached: {unique_id}")
        return text, None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raw = out.decode(errors="replace")[:200]
        logger.error(f"Deepgram parse error: {e}, raw: {raw}")
        return "", str(e)


CACHE_FILE = MEDIA_DIR / ".cache.json"


def _load_cache() -> dict[str, str]:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            return {k: v for k, v in data.items() if Path(v).exists()}
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, str]):
    CACHE_FILE.write_text(json.dumps(cache))


_file_cache: dict[str, str] = _load_cache()


async def download_file(file_id: str, name: str, unique_id: str = "") -> str | None:
    if unique_id and unique_id in _file_cache:
        cached = _file_cache[unique_id]
        if Path(cached).exists():
            logger.info(f"Cache hit: {unique_id} → {Path(cached).name}")
            return cached
        del _file_cache[unique_id]
    try:
        f = await bot.get_file(file_id)
        name = Path(name).name
        path = MEDIA_DIR / name
        if path.exists():
            stem = path.stem
            suffix = path.suffix
            i = 1
            while path.exists():
                path = MEDIA_DIR / f"{stem}_{i}{suffix}"
                i += 1
        await bot.download_file(f.file_path, str(path))
        if unique_id:
            _file_cache[unique_id] = str(path)
            _save_cache(_file_cache)
        return str(path)
    except Exception as e:
        logger.warning(f"download_file failed for {name}: {e}")
        return None


def _media_name(prefix: str, ext: str, msg: types.Message) -> str:
    ts = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else str(msg.message_id)
    return f"{prefix}_{ts}_{msg.message_id}{ext}"


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
    if DEBUG:
        logger.debug(f"Chat {chat_id} raw prompt: {full_prompt}")

    entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
    )
    await registry.get(chat_id).accept_entry(entry)


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


STREAM_DRAFT_INTERVAL = 0.3
_draft_counter = 0


def _next_draft_id() -> int:
    global _draft_counter
    _draft_counter += 1
    return _draft_counter


async def _clear_draft(chat_id: int, did: int):
    try:
        await bot(SendMessageDraft(chat_id=chat_id, draft_id=did, text=""))
    except Exception:
        pass


async def _ask(message: Optional[types.Message], prompt: str, chat_id: int):
    """Stream a Claude response. message may be None for reminder turns (uses bot.send_message)."""
    cid = chat_id
    typer = asyncio.create_task(typing_loop(cid))
    retries = 0

    # --- Streaming state ---
    parts: list[str] = []
    has_deltas = False
    draft_id = _next_draft_id()
    last_draft_time = 0.0
    last_draft_text = ""
    draft_has_text = False
    flood_cooldown_until = 0.0
    finalized: list[int] = []

    status: Optional[ToolStatusTracker] = None

    # For reminder turns (no message object), we send via bot.send_message
    async def _answer(text: str, **kwargs):
        if message is not None:
            return await message.answer(text, **kwargs)
        return await bot.send_message(cid, text, **kwargs)

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
            await bot(SendMessageDraft(chat_id=cid, draft_id=draft_id, text=text, parse_mode=parse_mode))
            draft_has_text = True
            last_draft_text = text
        except Exception as e:
            err_str = str(e)
            if final and ("can't parse entities" in err_str or "parse" in err_str.lower()):
                try:
                    await bot(SendMessageDraft(chat_id=cid, draft_id=draft_id, text=text, parse_mode=None))
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
            stream = get_session(cid).send_message(prompt).__aiter__()
            while True:
                try:
                    chunk = await stream.__anext__()
                except StopAsyncIteration:
                    break
                # Check cancel via ChatState
                cs = registry.get(cid)
                if cs.cancel_requested:
                    cs.cancel_requested = False
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
                        # For reminder turns, message may be None — ToolStatusTracker needs a message
                        # Use a sentinel: pass message if available, else None (tracker handles None)
                        status = ToolStatusTracker(bot, message, cid)
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
                        get_session(cid).reconnect()
                        retries += 1
                        if retries <= MAX_RETRIES and not finalized:
                            if message is not None:
                                await _send_safe(message, t(message, "reconnecting", n=retries))
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
                get_session(cid).reconnect()
                if message is not None:
                    await _send_safe(message, t(message, "error_retry", n=retries))
            else:
                parts.append(f"Error: {e}")
                break

    typer.cancel()

    # --- Finalize ---
    text = "".join(parts)
    logger.info(f"Chat {cid}: response {len(text)} chars, finalized={len(finalized)}, tools={len(status.tools) if status else 0}, draft_hanging={draft_has_text}")
    if DEBUG:
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
        await _answer(STRINGS["ru"]["empty"] if message is None else t(message, "empty"))


# --- Commands ---

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


async def set_commands():
    from aiogram.types import (
        BotCommandScopeAllPrivateChats,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeChat,
    )
    # Purge commands from every possible scope so stale lists don't show up
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

    # Also clear per-chat scopes for allowed users (chat-scoped commands override defaults)
    for uid in ALLOWED:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid), language_code="ru")
        except Exception as e:
            logger.debug(f"delete_my_commands chat={uid} failed: {e}")

    await bot.set_my_commands(COMMANDS_EN, scope=BotCommandScopeDefault())
    await bot.set_my_commands(COMMANDS_RU, scope=BotCommandScopeDefault(), language_code="ru")
    logger.info("Bot commands set (RU + EN), purged other scopes")


# --- Handlers ---

@dp.message(CommandStart())
async def h_start(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    s = registry.get(msg.chat.id).session
    sid = s.session_id
    await _send_safe(msg, t(msg, "start",
        session=sid[:8] + "..." if sid else "new",
        model=s.model,
        cwd=WORK_DIR,
        debounce=registry.get(msg.chat.id).debounce_sec,
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
    cs = registry.get(msg.chat.id)
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
    await _send_safe(msg, t(msg, "status",
        model=s.model,
        session=sid[:8] + "..." if sid else "none",
        cwd=WORK_DIR,
        debounce=cs.debounce_sec,
        debug="on" if DEBUG else "off",
        uptime=uptime_str(),
        context=ctx_str,
        rate_limit=rl_str,
        cost=f"{s.total_cost_usd:.4f}",
        media_count=media_count(),
        log_size=log_size(),
    ))


@dp.message(Command("clear"))
async def h_clear(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    cleared = await registry.get(cid).request_clear()
    if not cleared:
        await _send_safe(msg, t(msg, "clear_busy"))
        return
    await _send_safe(msg, t(msg, "cleared"))


@dp.message(Command("compact"))
async def h_compact(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    cs = registry.get(cid)
    if cs.is_busy:
        # Set flag — compaction will run after current processing ends
        await cs.request_compact()
        await _send_safe(msg, "⏳ Сейчас идёт обработка, сжатие запланировано после.")
        return
    await cs.request_compact()


@dp.message(Command("ping"))
async def h_ping(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    sid = registry.get(msg.chat.id).session.session_id
    await _send_safe(msg, t(msg, "ping", session=sid or "none"))


ALLOWED_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}


@dp.message(Command("model"))
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
        await registry.get(msg.chat.id).set_model(model_id, use_1m)
        ctx = "200K" if use_200k else "1M"
        await _send_safe(msg, t(msg, "model_set", model=f"{model_id} ({ctx})"))
    else:
        await _send_safe(msg, t(msg, "model_usage", model=registry.get(msg.chat.id).session.model))


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
                await registry.get(msg.chat.id).set_debounce(val)
                await _send_safe(msg, t(msg, "debounce_set", sec=val))
            else:
                await _send_safe(msg, "0-30 sec")
        except ValueError:
            await _send_safe(msg, t(msg, "debounce_usage", sec=registry.get(msg.chat.id).debounce_sec))
    else:
        await _send_safe(msg, t(msg, "debounce_usage", sec=registry.get(msg.chat.id).debounce_sec))


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


@dp.message(Command("restart"))
async def h_restart(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    await _send_safe(msg, t(msg, "restarting"))
    p = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "kesha-bot",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await p.communicate()


@dp.message(Command("stop"))
async def h_stop(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    stopped = await registry.get(cid).request_stop()
    if stopped:
        await _send_safe(msg, "Stopping...")
    else:
        await _send_safe(msg, "Nothing to stop.")


@dp.message(F.voice)
async def h_voice(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    cs = registry.get(chat_id)
    path = await download_file(msg.voice.file_id, _media_name("voice", ".oga", msg), msg.voice.file_unique_id)
    if not path:
        await enqueue(msg, "[voice: файл слишком большой]")
        return
    if not DEEPGRAM:
        await enqueue(msg, f"[voice: {path}]")
        return

    gen, media_gen = await cs.transcription_started()
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
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


@dp.message(F.media_group_id)
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


@dp.message(F.photo)
async def h_photo(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.photo[-1].file_id, _media_name("photo", ".jpg", msg), msg.photo[-1].file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}")


@dp.message(F.video_note)
async def h_video_note(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    cs = registry.get(chat_id)
    path = await download_file(msg.video_note.file_id, _media_name("videonote", ".mp4", msg), msg.video_note.file_unique_id)
    if not path:
        await enqueue(msg, "[video_note: файл слишком большой]")
        return
    if not DEEPGRAM:
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
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
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
    # ffmpeg failed or transcription empty — enqueue without transcript
    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[video_note: {path}]"
    fallback_entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
    )
    await cs.transcription_finished(fallback_entry, gen, media_gen)


@dp.message(F.document)
async def h_document(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    doc = msg.document
    ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
    path = await download_file(doc.file_id, doc.file_name or _media_name("doc", ext, msg), doc.file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[document: {path} ({doc.file_name})]" if path else f"[document: файл слишком большой ({doc.file_name})]"
    await enqueue(msg, f"{tag}{caption}")


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
    path = await download_file(msg.video.file_id, msg.video.file_name or _media_name("video", ".mp4", msg), msg.video.file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[video: {path}]" if path else "[video: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}")


@dp.message(F.audio)
async def h_audio(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    ext = os.path.splitext(msg.audio.file_name or "audio.mp3")[1] or ".mp3"
    name = msg.audio.file_name or _media_name("audio", ext, msg)
    path = await download_file(msg.audio.file_id, name, msg.audio.file_unique_id)
    tag = f"[audio: {path} ({name})]" if path else f"[audio: файл слишком большой ({name})]"
    await enqueue(msg, tag)


@dp.message(F.text)
async def h_text(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    await enqueue(msg, extract_text_with_urls(msg))


@dp.message()
async def h_fallback(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    text = msg.text or msg.caption or ""
    content_type = msg.content_type or "unknown"
    logger.warning(f"Chat {msg.chat.id}: unhandled message type={content_type}, text={text[:100]}")
    if text:
        await enqueue(msg, text)


# --- Main ---

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

    # Initialize ChatRegistry
    registry = ChatRegistry(
        bot=bot,
        mcp_config=_mcp_config,
        system_prompt=_system_prompt,
        model=MODEL,
        debounce_sec=DEBOUNCE_SEC,
        auto_compact_pct=AUTO_COMPACT_PCT,
        ask_fn=_ask,
        set_current_chat_fn=set_current_chat,
        get_lazy_block_fn=_reminders.get_lazy_block_for_prompt,
        compact_session_fn=_compact.compact_session,
        maybe_auto_compact_fn=_compact.maybe_auto_compact,
        work_dir=WORK_DIR,
    )

    cleanup_media()
    cleanup_logs()
    asyncio.create_task(daily_cleanup_loop())
    await set_commands()
    logger.info(f"Kesha bot | CWD={WORK_DIR} | Model={MODEL} | Debug={DEBUG}")
    logger.info(f"Allowed: {ALLOWED or 'all'} | Media: {MEDIA_DIR} | Logs: {LOG_DIR}")

    async def _urgent_llm_handler(chat_id: int, prompt: str):
        """Handle urgent_llm reminders through ChatState pipeline."""
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
