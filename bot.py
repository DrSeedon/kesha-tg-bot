"""Kesha Telegram Bot — Claude Agent SDK with persistent sessions."""

import asyncio
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram_media_group import media_group_handler
from aiogram.filters import CommandStart, Command
from aiogram.methods import SendMessageDraft
from aiogram.types import BotCommand, BotCommandScopeDefault
from dotenv import load_dotenv

from claude_session import ClaudeSession
from kesha_tools import kesha_server, set_bot_ref
import reminders as _reminders

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
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./storage/media")).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs")).resolve()
GREET_FLAG = Path(__file__).parent / "storage" / "greet_on_restart"
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
        if f.is_file() and f.name != ".cache.json" and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old media files")
        _file_cache = {k: v for k, v in _file_cache.items() if Path(v).exists()}
        _save_cache(_file_cache)


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


_sessions: dict[int, ClaudeSession] = {}
_mcp_config = _load_global_mcp()
_system_prompt = load_system_prompt()

def get_session(chat_id: int) -> ClaudeSession:
    if chat_id not in _sessions:
        from pathlib import Path as _P
        session_file = _P(__file__).parent / "storage" / "sessions" / str(chat_id)
        _sessions[chat_id] = ClaudeSession(
            cwd=WORK_DIR,
            model=MODEL,
            system_prompt=_system_prompt,
            mcp_servers=_mcp_config,
            session_file=session_file,
        )
        logger.info(f"Created new ClaudeSession for chat {chat_id}")
    return _sessions[chat_id]

# Backward compat: default session for tools/reminders that use global `claude`
claude = get_session(list(ALLOWED)[0] if ALLOWED else 0)

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


# --- Debounce + Queue ---

_pending: dict[int, list[dict]] = {}
_pending_timers: dict[int, asyncio.Task] = {}
_processing: set[int] = set()
_cancel: set[int] = set()
_queue: dict[int, list[list[dict]]] = {}
_pending_transcriptions: dict[int, int] = {}  # chat_id -> count of pending transcriptions
current_batch_message_ids: dict[int, list[int]] = {}
active_chat_id: int | None = None


def _make_entry(msg: types.Message, prompt: str) -> dict:
    return {"msg": msg, "prompt": prompt}


TRANSCRIPTION_WAIT_MAX = 30  # max seconds to wait for pending transcriptions


async def _debounce_fire(chat_id: int):
    await asyncio.sleep(DEBOUNCE_SEC)
    # Wait for pending transcriptions (voice/video_note being transcribed)
    waited = 0
    while _pending_transcriptions.get(chat_id, 0) > 0 and waited < TRANSCRIPTION_WAIT_MAX:
        await asyncio.sleep(0.5)
        waited += 0.5
    if waited > 0:
        logger.info(f"Chat {chat_id}: waited {waited:.1f}s for transcriptions")
    batch = _pending.pop(chat_id, [])
    _pending_timers.pop(chat_id, None)
    if not batch:
        return

    if chat_id in _processing:
        combined = "\n\n".join(e["prompt"] for e in batch)
        new_ids = [e["msg"].message_id for e in batch]
        current_batch_message_ids.setdefault(chat_id, []).extend(new_ids)
        logger.info(f"Chat {chat_id}: injecting {len(batch)} msgs while processing ({len(combined)} chars)")
        await get_session(chat_id).inject(combined)
        return

    await _process_batch(chat_id, batch)


async def _process_batch(chat_id: int, batch: list[dict]):
    global active_chat_id
    _processing.add(chat_id)
    active_chat_id = chat_id
    current_batch_message_ids[chat_id] = [e["msg"].message_id for e in batch]
    try:
            last_msg = batch[-1]["msg"]
            from datetime import timezone, timedelta
            krsk = timezone(timedelta(hours=7))
            batch_time = batch[0]["msg"].date.astimezone(krsk).strftime("%Y-%m-%d %H:%M %z")
            time_prefix = f"[{batch_time}] "
            if len(batch) == 1:
                combined = time_prefix + f"[msg_id={batch[0]['msg'].message_id}] " + batch[0]["prompt"]
            else:
                combined = "\n\n".join(
                    f"--- message {i+1}/{len(batch)} [msg_id={e['msg'].message_id}] ---\n{e['prompt']}"
                    for i, e in enumerate(batch)
                )
                combined = time_prefix + combined

            try:
                lazy_block = _reminders.get_lazy_block_for_prompt(chat_id)
                if lazy_block:
                    combined = lazy_block + combined
                    logger.info(f"Chat {chat_id}: injected lazy reminders block ({len(lazy_block)} chars)")
            except Exception as e:
                logger.error(f"Lazy reminder injection failed: {e}")

            previews = []
            for e in batch:
                p = e["prompt"]
                if "[photo:" in p:
                    previews.append("photo")
                elif "[voice:" in p:
                    previews.append("voice")
                elif "[video_note:" in p:
                    previews.append("videonote")
                elif "[video:" in p:
                    previews.append("video")
                elif "[document:" in p:
                    previews.append("doc")
                elif "[audio:" in p:
                    previews.append("audio")
                elif "[sticker:" in p:
                    previews.append("sticker")
                else:
                    txt = p.split("]: ", 1)[-1][:40].replace("\n", " ")
                    previews.append(f'"{txt}"')
            logger.info(f"Chat {chat_id}: sending {len(batch)} msgs [{', '.join(previews)}] ({len(combined)} chars)")
            if DEBUG:
                logger.debug(f"Chat {chat_id} full prompt:\n{combined}")

            await _ask(last_msg, combined)
    except Exception as e:
        logger.error(f"Chat {chat_id} batch error: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id, f"Bot error: {e}", parse_mode=None)
        except Exception:
            pass
    finally:
        _processing.discard(chat_id)
        queued = _queue.pop(chat_id, None)
        pending = _pending.pop(chat_id, [])
        timer = _pending_timers.pop(chat_id, None)
        if timer and not timer.done():
            timer.cancel()
        if queued or pending:
            merged = []
            for b in (queued or []):
                merged.extend(b)
            merged.extend(pending)
            if merged:
                asyncio.create_task(_process_batch(chat_id, merged))


async def enqueue(msg: types.Message, prompt: str):
    chat_id = msg.chat.id
    prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}{prompt}"

    mg = msg.media_group_id
    logger.info(f"Chat {chat_id}: received from {msg.from_user.id} (media_group={mg})")
    if DEBUG:
        logger.debug(f"Chat {chat_id} raw prompt: {prompt}")

    _pending.setdefault(chat_id, []).append(_make_entry(msg, prompt))

    old_timer = _pending_timers.get(chat_id)
    if old_timer and not old_timer.done():
        old_timer.cancel()

    _pending_timers[chat_id] = asyncio.create_task(_debounce_fire(chat_id))


async def _send_safe(message: types.Message, text: str):
    try:
        return await message.answer(text)
    except Exception:
        return await message.answer(text, parse_mode=None)


STREAM_DRAFT_INTERVAL = 0.3
_draft_counter = 0


def _next_draft_id() -> int:
    global _draft_counter
    _draft_counter += 1
    return _draft_counter


async def _clear_draft(chat_id: int, did: int):
    try:
        await bot(SendMessageDraft(chat_id=chat_id, draft_id=did, text=" "))
    except Exception:
        pass


async def _ask(message: types.Message, prompt: str):
    cid = message.chat.id
    typer = asyncio.create_task(typing_loop(cid))
    retries = 0

    # --- Streaming state ---
    parts = []              # text chunks for current block
    has_deltas = False
    draft_id = _next_draft_id()
    last_draft_time = 0.0
    last_draft_len = 0

    current_msg = None      # real TG message being edited (text or tool)
    current_is_tool = False  # what's in current_msg right now
    finalized = []          # message_ids that are done, don't touch

    async def _draft_update():
        nonlocal last_draft_time, last_draft_len
        text = "".join(parts)
        if not text:
            return
        now = time.time()
        if (now - last_draft_time) < STREAM_DRAFT_INTERVAL:
            return
        if len(text) == last_draft_len:
            return
        try:
            if current_msg and current_is_tool:
                await bot.edit_message_text(text[:TG_MSG_LIMIT], chat_id=cid, message_id=current_msg.message_id, parse_mode=None)
            else:
                await bot(SendMessageDraft(chat_id=cid, draft_id=draft_id, text=text[:TG_MSG_LIMIT]))
        except Exception as e:
            logger.debug(f"Draft update error: {e}")
        last_draft_time = now
        last_draft_len = len(text)

    async def _finalize_current_text():
        """Delete draft/stream msg and send clean new one with proper formatting."""
        nonlocal current_msg, current_is_tool, parts, has_deltas, draft_id, last_draft_time, last_draft_len
        text = "".join(parts)
        if not text:
            return
        # Delete the old draft/stream message
        if current_msg:
            try:
                await bot.delete_message(chat_id=cid, message_id=current_msg.message_id)
            except Exception as e:
                logger.debug(f"Could not delete draft msg: {e}")
            current_msg = None
        # Send clean new message with proper markdown formatting
        for p in split_msg(text):
            m = await _send_safe(message, p)
            if m:
                finalized.append(m.message_id)
        parts = []
        has_deltas = False
        current_is_tool = False
        draft_id = _next_draft_id()
        last_draft_time = 0.0
        last_draft_len = 0

    while retries <= MAX_RETRIES:
        try:
            async for chunk in get_session(cid).send_message(prompt):
                if cid in _cancel:
                    _cancel.discard(cid)
                    if parts:
                        parts.append("\n\n_(stopped)_")
                    break
                ct = chunk["type"]
                if ct == "text_delta":
                    if not has_deltas and current_msg and current_is_tool:
                        # First text after tool — will reuse tool msg via edit
                        pass
                    has_deltas = True
                    parts.append(chunk["content"])
                    await _draft_update()
                elif ct == "text" and not has_deltas:
                    parts.append(chunk["content"])
                elif ct == "tool":
                    tool_name = chunk.get("name", "?")
                    tool_input = chunk.get("input", {})
                    tool_hint = ""
                    if isinstance(tool_input, dict):
                        if "command" in tool_input:
                            tool_hint = f" `{str(tool_input['command'])[:60]}`"
                        elif "file_path" in tool_input:
                            tool_hint = f" `{tool_input['file_path']}`"
                        elif "pattern" in tool_input:
                            tool_hint = f" `{tool_input['pattern']}`"
                        elif "prompt" in tool_input:
                            tool_hint = f" `{str(tool_input['prompt'])[:40]}`"
                    logger.info(f"Chat {cid} tool: {tool_name}{tool_hint}")
                    tool_text = f"🔧 {tool_name}{tool_hint}"

                    if has_deltas and parts:
                        # Had text streaming — finalize it first
                        await _finalize_current_text()
                        # New message for tool
                        current_msg = await message.answer(tool_text, parse_mode=None)
                        current_is_tool = True
                    elif current_msg and current_is_tool:
                        # Already showing a tool — just edit it
                        try:
                            await bot.edit_message_text(tool_text, chat_id=cid, message_id=current_msg.message_id, parse_mode=None)
                        except Exception:
                            pass
                    else:
                        # First thing or after nothing — new message
                        if current_msg:
                            finalized.append(current_msg.message_id)
                        current_msg = await message.answer(tool_text, parse_mode=None)
                        current_is_tool = True
                elif ct == "turn_done":
                    if parts:
                        await _finalize_current_text()
                elif ct == "error":
                    err = chunk["content"]
                    if "session" in err.lower() or "process" in err.lower():
                        logger.warning(f"Session error, reconnecting: {err}")
                        get_session(cid).reconnect()
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
                get_session(cid).reconnect()
                await _send_safe(message, t(message, "error_retry", n=retries))
            else:
                parts.append(f"Error: {e}")
                break

    typer.cancel()

    # --- Finalize ---
    text = "".join(parts)
    logger.info(f"Chat {cid}: response {len(text)} chars, finalized={len(finalized)}")
    if DEBUG:
        logger.debug(f"Chat {cid} full response: {text[:500]}")

    if text:
        if current_msg and current_is_tool:
            # Text after last tool — replace tool msg with final text
            if len(text) <= TG_MSG_LIMIT:
                try:
                    await bot.edit_message_text(text, chat_id=cid, message_id=current_msg.message_id)
                except Exception:
                    try:
                        await bot.edit_message_text(text, chat_id=cid, message_id=current_msg.message_id, parse_mode=None)
                    except Exception:
                        pass
            else:
                try:
                    await bot.delete_message(cid, current_msg.message_id)
                except Exception:
                    pass
                for p in split_msg(text):
                    await _send_safe(message, p)
        elif current_msg:
            # Was streaming text into draft — send as final msg
            if len(text) <= TG_MSG_LIMIT:
                try:
                    await bot.edit_message_text(text, chat_id=cid, message_id=current_msg.message_id)
                except Exception:
                    try:
                        await bot.edit_message_text(text, chat_id=cid, message_id=current_msg.message_id, parse_mode=None)
                    except Exception:
                        pass
            else:
                try:
                    await bot.delete_message(cid, current_msg.message_id)
                except Exception:
                    pass
                for p in split_msg(text):
                    await _send_safe(message, p)
        else:
            for p in split_msg(text):
                await _send_safe(message, p)
    elif current_msg and current_is_tool:
        # Ended on a tool with no text after — delete tool indicator
        try:
            await bot.delete_message(cid, current_msg.message_id)
        except Exception:
            pass
    elif not finalized:
        await _send_safe(message, t(message, "empty"))


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
    BotCommand(command="restart", description="Перезапустить бота"),
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
    BotCommand(command="restart", description="Restart bot"),
]


async def set_commands():
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code="ru")
    await bot.set_my_commands(COMMANDS_EN, scope=BotCommandScopeDefault())
    await bot.set_my_commands(COMMANDS_RU, scope=BotCommandScopeDefault(), language_code="ru")
    logger.info("Bot commands set (RU + EN)")


# --- Handlers ---

@dp.message(CommandStart())
async def h_start(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    s = get_session(msg.chat.id)
    sid = s.session_id
    await _send_safe(msg, t(msg, "start",
        session=sid[:8] + "..." if sid else "new",
        model=s.model,
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
    s = get_session(msg.chat.id)
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
        debounce=DEBOUNCE_SEC,
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
    get_session(msg.chat.id).reset()
    await _send_safe(msg, t(msg, "cleared"))


@dp.message(Command("ping"))
async def h_ping(msg: types.Message):
    await _send_safe(msg, t(msg, "ping", session=get_session(msg.chat.id).session_id or "none"))


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
        s = get_session(msg.chat.id)
        s.use_1m = not use_200k
        s.model = model_id
        ctx = "200K" if use_200k else "1M"
        await _send_safe(msg, t(msg, "model_set", model=f"{s.model} ({ctx})"))
    else:
        await _send_safe(msg, t(msg, "model_usage", model=get_session(msg.chat.id).model))


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
    if cid in _processing:
        _cancel.add(cid)
        await get_session(cid).interrupt()
        await _send_safe(msg, "Stopping...")
    else:
        await _send_safe(msg, "Nothing to stop.")


@dp.message(F.voice)
async def h_voice(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    path = await download_file(msg.voice.file_id, _media_name("voice", ".oga", msg), msg.voice.file_unique_id)
    if not path:
        await enqueue(msg, "[voice: файл слишком большой]")
        return
    _pending_transcriptions[chat_id] = _pending_transcriptions.get(chat_id, 0) + 1
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        text, err = await transcribe(path, msg.voice.file_unique_id or "")
    finally:
        _pending_transcriptions[chat_id] = max(0, _pending_transcriptions.get(chat_id, 1) - 1)
    if not text:
        err_msg = t(msg, "voice_fail")
        if err:
            err_msg += f" ({err})"
        return await _send_safe(msg, err_msg)
    await enqueue(msg, f"[voice: {path} | {text}]")


@dp.message(F.media_group_id, F.photo)
@media_group_handler
async def h_photo_album(messages: list[types.Message]):
    if not allowed(messages[0].from_user.id):
        return
    parts = []
    for m in messages:
        path = await download_file(m.photo[-1].file_id, _media_name("photo", ".jpg", m), m.photo[-1].file_unique_id)
        tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
        parts.append(tag)
    caption = ""
    for m in messages:
        if m.caption:
            caption = f"\n{m.caption}"
            break
    fwd = forward_meta(messages[0])
    media_block = "\n".join(parts)
    await enqueue(messages[0], f"{fwd}{media_block}{caption}")


@dp.message(F.media_group_id, F.video)
@media_group_handler
async def h_video_album(messages: list[types.Message]):
    if not allowed(messages[0].from_user.id):
        return
    parts = []
    for m in messages:
        path = await download_file(m.video.file_id, _media_name("video", ".mp4", m), m.video.file_unique_id)
        tag = f"[video: {path}]" if path else "[video: файл слишком большой]"
        parts.append(tag)
    caption = ""
    for m in messages:
        if m.caption:
            caption = f"\n{m.caption}"
            break
    fwd = forward_meta(messages[0])
    media_block = "\n".join(parts)
    await enqueue(messages[0], f"{fwd}{media_block}{caption}")


@dp.message(F.media_group_id, F.document)
@media_group_handler
async def h_document_album(messages: list[types.Message]):
    if not allowed(messages[0].from_user.id):
        return
    parts = []
    for m in messages:
        doc = m.document
        ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
        path = await download_file(doc.file_id, doc.file_name or _media_name("doc", ext, m), doc.file_unique_id)
        tag = f"[document: {path} ({doc.file_name})]" if path else f"[document: файл слишком большой ({doc.file_name})]"
        parts.append(tag)
    caption = ""
    for m in messages:
        if m.caption:
            caption = f"\n{m.caption}"
            break
    fwd = forward_meta(messages[0])
    media_block = "\n".join(parts)
    await enqueue(messages[0], f"{fwd}{media_block}{caption}")


@dp.message(F.photo)
async def h_photo(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    path = await download_file(msg.photo[-1].file_id, _media_name("photo", ".jpg", msg), msg.photo[-1].file_unique_id)
    caption = f"\n{msg.caption}" if msg.caption else ""
    tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}")


@dp.message(F.video_note)
async def h_video_note(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    path = await download_file(msg.video_note.file_id, _media_name("videonote", ".mp4", msg), msg.video_note.file_unique_id)
    if not path:
        await enqueue(msg, "[video_note: файл слишком большой]")
        return
    if DEEPGRAM:
        _pending_transcriptions[chat_id] = _pending_transcriptions.get(chat_id, 0) + 1
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
            finally:
                _pending_transcriptions[chat_id] = max(0, _pending_transcriptions.get(chat_id, 1) - 1)
            if text:
                await enqueue(msg, f"[video_note: {path} | {text}]")
                return
        else:
            _pending_transcriptions[chat_id] = max(0, _pending_transcriptions.get(chat_id, 1) - 1)
    await enqueue(msg, f"[video_note: {path}]")


@dp.message(F.document)
async def h_document(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    doc = msg.document
    ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
    path = await download_file(doc.file_id, doc.file_name or _media_name("doc", ext, msg), doc.file_unique_id)
    caption = f"\n{msg.caption}" if msg.caption else ""
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
    caption = f"\n{msg.caption}" if msg.caption else ""
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
    await enqueue(msg, msg.text)


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


async def main():
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    cleanup_media()
    await set_commands()
    logger.info(f"Kesha bot | CWD={WORK_DIR} | Model={MODEL} | Debug={DEBUG}")
    logger.info(f"Allowed: {ALLOWED or 'all'} | Media: {MEDIA_DIR} | Logs: {LOG_DIR}")
    async def _urgent_llm_handler(chat_id: int, prompt: str):
        """Handle urgent_llm reminders through normal _ask pipeline with retry."""
        from datetime import datetime as dt, timezone as tz, timedelta as td
        max_retries = 3
        for attempt in range(max_retries):
            try:
                krsk = tz(td(hours=7))
                now_str = dt.now(tz=krsk).strftime("%Y-%m-%d %H:%M %z")
                full_prompt = f"[{now_str}] " + prompt

                _processing.add(chat_id)
                current_batch_message_ids[chat_id] = []
                try:
                    await bot.send_chat_action(chat_id, "typing")
                    tmp = await bot.send_message(chat_id, "💭")
                    await _ask(tmp, full_prompt)
                finally:
                    _processing.discard(chat_id)
                return
            except Exception as e:
                logger.error(f"urgent_llm handler error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    delay = 15 * (attempt + 1)
                    logger.info(f"urgent_llm retry in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"urgent_llm all {max_retries} attempts failed, sending raw fallback")
                    for fb_attempt in range(3):
                        try:
                            await bot.send_message(chat_id, f"⏰ {prompt}", parse_mode=None)
                            return
                        except Exception:
                            await asyncio.sleep(10)
                    logger.error(f"urgent_llm fallback also failed for reminder in chat {chat_id}")

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

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
