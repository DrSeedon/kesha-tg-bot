"""Kesha bot — constants, environment, logging setup, i18n strings."""

import logging
import os
import sys
from datetime import datetime, time as dt_time, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import types
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}
WORK_DIR = os.getenv("WORK_DIR", ".")
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
RUNTIME = os.getenv("KESHA_RUNTIME", "claude")

# Model per runtime. Claude's id would be rejected by Codex and vice versa, so a
# switch must carry its own. Verified live: gpt-5.1-codex-max is NOT available
# on a ChatGPT subscription (HTTP 400); gpt-5.6-sol is.
RUNTIME_MODELS = {
    "codex": os.getenv("KESHA_CODEX_MODEL", "gpt-5.6-sol"),
}
DEEPGRAM = os.getenv("DEEPGRAM_API_KEY", "")
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
MAX_RETRIES = 2
DEBOUNCE_SEC = int(os.getenv("DEBOUNCE_SEC", "3"))
AUTO_COMPACT_TZ = ZoneInfo("Asia/Krasnoyarsk")
AUTO_COMPACT_WINDOW_START = dt_time(23, 0)
AUTO_COMPACT_WINDOW_END = dt_time(8, 0)
AUTO_COMPACT_IDLE = timedelta(minutes=55)
AUTO_COMPACT_MIN_CONTEXT_PCT = 20.0
TG_MSG_LIMIT = 4096
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./storage/media")).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs")).resolve()
GREET_FLAG = Path(__file__).parent / "storage" / "greet_on_restart"
MEDIA_MAX_AGE_H = 24
MEDIA_MAX_MB = int(os.getenv("MEDIA_MAX_MB", "100"))

NOTIFY_CHAT = int(os.getenv("NOTIFY_CHAT", "0")) or (list(ALLOWED)[0] if ALLOWED else 0)

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

    if not os.environ.get("KESHA_NO_FILE_LOG"):
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
        "debug_on": "🐛 Debug включён. Логи: `{path}`",
        "debug_off": "🐛 Debug выключен.",
        "debounce_set": "⏱ Debounce: `{sec}s`",
        "debounce_usage": "Текущий: `{sec}s`\nИспользование: `/debounce 5`",
        "reconnecting": "⚠️ Переподключаюсь... (попытка {n})",
        "error_retry": "⚠️ Ошибка, перезапуск сессии (попытка {n})...",
        "session_limit": "⏳ Достигнут лимит подписки {runtime}{reset}. Жду сброса — напиши позже.{quota}",
        "context_limit": "🧠 Контекст заполнен. Отправь /compact, чтобы продолжить без очистки.",
        "context_reserve": "🧠 Контекст почти заполнен. Отправь /compact, затем повтори сообщение.",
        "context_unknown": "⚠️ Не удалось проверить свободный контекст. Повтори сообщение чуть позже.",
        "context_usage_limit": "🚧 Упёрлись в лимит подписки {runtime}{reset}. Подожди сброса и повтори — контекст тут ни при чём.{quota}",
        "context_runtime_invariant": "⚠️ Рантайм не совпал с ожидаемой конфигурацией (ждём {expected}: 1M контекст, 64k вывода, авто-компакт выключен). Сообщение не отправлено — проверь конфиг.",
        "context_runtime_unhealthy": "⚠️ Рантайм не отвечает — не смог проверить свободный контекст даже со второй попытки, поэтому сообщение не отправил. Клиент переподключён, отправь ещё раз.",
        "session_unavailable": "⚠️ Сохранённая сессия недоступна. Отправь /clear, чтобы начать новую.",
        "compact_native_start": "🗜 Сжимаю контекст средствами {runtime} (было {before:.0f}%)...",
        "compact_native_done": "✅ Контекст сжат средствами {runtime}: {before:.0f}% → {after:.0f}% ({tokens} токенов).",
        "compact_native_failed": "⚠️ Родное сжатие не удалось: {error}",
        "runtime_status": "🧩 Рантайм: *{current}* ({model})\nДоступны: {available}\n{quota}\nКоманды: /claude · /codex",
        "runtime_quota_unknown": "📊 Лимит: неизвестен",
        # Окно квоты: утилизация% (сколько % окна прошло) осталось-до-сброса · темп
        "quota_pace_ok": "темп ok",
        "quota_pace_over": "темп +{value}",
        "runtime_switched": "🔄 Рантайм: *{previous}* → *{current}* ({model}).\n{handoff}",
        "runtime_handoff_ok": "Последние видимые сообщения переданы в новый рантайм.",
        "runtime_handoff_none": "История не передана (пустая или недоступна) — начинаю с чистого листа.",
        "runtime_handoff_unsupported": "История не передана: рантайм не поддерживает безопасный пассивный перенос.",
        "runtime_same": "🧩 Уже на *{current}*.",
        "runtime_unknown": "⚠️ Неизвестный рантайм `{runtime}`. Доступны: {available}",
        "runtime_busy": "⏳ Сейчас идёт обработка — переключение возможно только когда я свободен. Дождись ответа или отправь /stop.",
        "runtime_failed": "⚠️ Не удалось переключиться на *{runtime}*: {error}\nОстаюсь на *{fallback}* — продолжаю работать.",
        "clear_failed": "⚠️ Не удалось полностью очистить сессии: {error}",
        "compact_floor": "⚠️ Для безопасного /compact уже не хватает свободного контекста. Сессия сохранена; доступен только /clear.",
        "activity_retry": "⚠️ Не удалось надёжно сохранить сообщение. Отправь его ещё раз.",
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
            "📝 Лог: `{log_size}`{quota}"
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
        "debug_on": "🐛 Debug enabled. Logs: `{path}`",
        "debug_off": "🐛 Debug disabled.",
        "debounce_set": "⏱ Debounce: `{sec}s`",
        "debounce_usage": "Current: `{sec}s`\nUsage: `/debounce 5`",
        "reconnecting": "⚠️ Reconnecting... (attempt {n})",
        "error_retry": "⚠️ Error, restarting session (attempt {n})...",
        "session_limit": "⏳ {runtime} subscription limit reached{reset}. Waiting for reset — message me later.{quota}",
        "context_limit": "🧠 Context is full. Send /compact to continue without clearing it.",
        "context_reserve": "🧠 Context is almost full. Send /compact, then resend your message.",
        "context_unknown": "⚠️ Could not verify free context. Please resend the message shortly.",
        "context_usage_limit": "🚧 Hit the {runtime} subscription limit{reset}. Wait for the reset and resend — this is not a context problem.{quota}",
        "context_runtime_invariant": "⚠️ The runtime did not match the expected configuration (want {expected}: 1M context, 64k output, auto-compact off). Message not sent — check the config.",
        "context_runtime_unhealthy": "⚠️ The runtime is not responding — I could not verify free context even on a second attempt, so I did not send your message. The client has been reconnected; please send it again.",
        "session_unavailable": "⚠️ The saved session is unavailable. Send /clear to start a new one.",
        "compact_native_start": "🗜 Compacting context via {runtime} (was {before:.0f}%)...",
        "compact_native_done": "✅ Context compacted via {runtime}: {before:.0f}% → {after:.0f}% ({tokens} tokens).",
        "compact_native_failed": "⚠️ Native compaction failed: {error}",
        "runtime_status": "🧩 Runtime: *{current}* ({model})\nAvailable: {available}\n{quota}\nCommands: /claude · /codex",
        "runtime_quota_unknown": "📊 Quota: unknown",
        "quota_pace_ok": "pace ok",
        "quota_pace_over": "pace +{value}",
        "runtime_switched": "🔄 Runtime: *{previous}* → *{current}* ({model}).\n{handoff}",
        "runtime_handoff_ok": "Recent visible messages were carried to the new runtime.",
        "runtime_handoff_none": "History not carried over (empty or unavailable) — starting fresh.",
        "runtime_handoff_unsupported": "History not carried over: this runtime has no safe passive handoff.",
        "runtime_same": "🧩 Already on *{current}*.",
        "runtime_unknown": "⚠️ Unknown runtime `{runtime}`. Available: {available}",
        "runtime_busy": "⏳ A turn is in progress — switching is only possible when I am idle. Wait for the answer or send /stop.",
        "runtime_failed": "⚠️ Could not switch to *{runtime}*: {error}\nStaying on *{fallback}* — still working.",
        "clear_failed": "⚠️ Could not fully clear the sessions: {error}",
        "compact_floor": "⚠️ There is not enough free context for safe /compact. The session is preserved; only /clear remains available.",
        "activity_retry": "⚠️ Could not safely save the message. Please send it again.",
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
            "📝 Log: `{log_size}`{quota}"
        ),
    },
}


class _Blank(dict):
    """Missing placeholders render as empty, never as an exception.

    These strings are terminal notices: they exist to explain why a message
    could not be answered. A KeyError here would swallow the explanation and
    leave the user with silence — the exact failure the message prevents.
    """

    def __missing__(self, key):
        return ""


def render(key: str, lang: str = "ru", **kw) -> str:
    """Format a STRINGS entry, tolerating callers that omit optional fields."""
    if lang not in STRINGS:
        lang = "en"
    return STRINGS[lang][key].format_map(_Blank(kw))


def lang_of(msg) -> str:
    """Locale of a message, or the default when there is no message at all
    (reminders and other bot-initiated turns have no sender).

    Never raises: a message with no sender (channel posts) must not take down
    the rendering of a terminal notice — losing the explanation is worse than
    picking the wrong locale for it.
    """
    sender = getattr(msg, "from_user", None)
    if sender is None:
        return "ru"
    return (sender.language_code or "en")[:2]


def t(msg: types.Message, key: str, **kw) -> str:
    return render(key, lang_of(msg), **kw)


# --- System Prompt ---

SYSTEM_PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"


def load_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        raw = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
        return raw.format(cwd=WORK_DIR, media_dir=MEDIA_DIR)
    return ""
