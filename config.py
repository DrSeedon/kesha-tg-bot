"""Kesha bot — constants, environment, logging setup, i18n strings."""

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiogram import types
from dotenv import load_dotenv

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

ALLOWED_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}

# --- Logging ---

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("kesha")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

import time as _time_mod
_time_mod.tzset() if hasattr(_time_mod, 'tzset') else None
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
_KRSK = timezone(timedelta(hours=7))


def _krsk_time(record, datefmt=None):
    dt = datetime.fromtimestamp(record.created, tz=_KRSK)
    if datefmt:
        return dt.strftime(datefmt)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"


_fmt.formatTime = _krsk_time  # type: ignore[assignment]

if not logger.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    logger.addHandler(_sh)

    if __name__ == "__main__" or os.environ.get("KESHA_MAIN") == "1":
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
            "/debug — toggle debug logs\n"
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
