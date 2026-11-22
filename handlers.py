"""All @dp.message handlers, command lists, set_commands(). Call register(dp) to attach."""

import asyncio
import logging
import os

from aiogram import Dispatcher, F, types
from aiogram.enums import ChatAction, ContentType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram_media_group import media_group_handler

from chat_state import PendingEntry
from model_choices import MODELS, resolve_choice
import config as _cfg
from config import (
    ALLOWED,
    BOT_SERVICE_UNIT,
    OWNER_ID,
    LOG_DIR,
    STRINGS,
    WORK_DIR,
    lang_of,
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
from message_log import ActivityPersistenceError
from limits import (
    LimitsUnavailable,
    fetch_limits_card,
    fetch_limits_usage,
    format_limits_message,
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
    return bool(ALLOWED) and uid in ALLOWED


def is_owner(uid: int | None) -> bool:
    return uid is not None and bool(OWNER_ID) and uid == OWNER_ID


def _interactive_sender(msg) -> bool:
    user = msg.from_user
    return user is not None and not getattr(user, "is_bot", False) and allowed(user.id)


async def _deny_once(msg: types.Message):
    uid = msg.from_user.id
    if uid not in _denied_notified:
        _denied_notified.add(uid)
        await _send_safe(msg, t(msg, "no_access", uid=uid))


async def enqueue(msg: types.Message, prompt: str, *, media=None, bare_photo: bool = False, audio_paths=()):
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
        bare_photo=bare_photo,
        interactive_user=_interactive_sender(msg),
        audio_paths=audio_paths,
    )
    try:
        if media is None:
            await _registry.get(chat_id).accept_entry(entry)
        else:
            state, generation, media_generation = media
            await state.media_finished(entry, generation, media_generation)
    except ValueError as exc:
        if str(exc) != "antigravity_interactive_only":
            raise
        await _send_safe(msg, "Antigravity принимает сообщения от разрешённых пользователей Telegram.")
    except ActivityPersistenceError:
        logger.error(f"Chat {chat_id}: activity admission failed")
        await _send_safe(msg, t(msg, "activity_retry"))


async def _start_media(msg: types.Message):
    state = _registry.get(msg.chat.id)
    try:
        generation, media_generation = await state.media_started()
    except ActivityPersistenceError:
        logger.error(f"Chat {msg.chat.id}: media activity admission failed")
        await _send_safe(msg, t(msg, "activity_retry"))
        return None
    return state, generation, media_generation


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
        cost=f"{s.total_cost_usd:.4f}" if s.total_cost_usd is not None else "n/a",
        media_count=media_count(),
        log_size=log_size(),
    ))


async def h_limits(msg: types.Message):
    """Send Orchestra's limits, with its card as optional presentation."""
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    if msg.chat.type != "private":
        return
    try:
        usage = await fetch_limits_usage()
        response = format_limits_message(usage)
    except Exception as exc:
        detail = str(exc).strip() or "(без сообщения)"
        logger.error("/limits usage failed: %s: %s", type(exc).__name__, detail)
        await _send_safe(msg, f"❌ /limits: {type(exc).__name__}: {detail}")
        return

    try:
        image = await fetch_limits_card()
        if not image:
            raise LimitsUnavailable("Orchestra вернула пустую карточку лимитов")
        await msg.answer_photo(
            photo=BufferedInputFile(image, filename="limits.png"),
            caption=response,
        )
    except Exception as exc:
        detail = str(exc).strip() or "(без сообщения)"
        logger.warning("/limits card unavailable: %s: %s", type(exc).__name__, detail)
        await _send_safe(msg, response)


async def h_clear(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    try:
        cleared = await _registry.get(cid).request_clear()
    except ActivityPersistenceError:
        await _send_safe(msg, t(msg, "activity_retry"))
        return
    except Exception as exc:
        logger.exception(f"Chat {cid}: clear failed: {exc}")
        await _send_safe(msg, t(msg, "clear_failed", error=str(exc)[:300]))
        return
    if not cleared:
        await _send_safe(msg, t(msg, "clear_busy"))
        return
    await _send_safe(msg, t(msg, "cleared"))


async def h_compact(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    cs = _registry.get(cid)
    options = {}
    if cs.runtime_id == "antigravity":
        if not _interactive_sender(msg):
            return
        options["interactive_user"] = True
    if cs.is_busy:
        try:
            await cs.request_compact(**options)
        except ActivityPersistenceError:
            await _send_safe(msg, t(msg, "activity_retry"))
            return
        await _send_safe(msg, "⏳ Сейчас идёт обработка, сжатие запланировано после.")
        return
    try:
        await cs.request_compact(**options)
    except ActivityPersistenceError:
        await _send_safe(msg, t(msg, "activity_retry"))


async def h_runtime(msg: types.Message):
    """Show or switch the chat's runtime. Everyone in ALLOWED may use it.

    Deliberately not owner-gated: the project has no owner concept, only
    ALLOWED, and adding a second authorization class for one command would be a
    new way to do access control. The shared-budget concern is answered by
    visibility instead — every switch announces itself in the chat.
    """
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes

    target = (msg.text or "").split(maxsplit=1)
    argument = target[1].strip().lower() if len(target) > 1 else ""

    if not argument:
        await _send_runtime_panel(msg)
        return

    await _switch_runtime_command(msg, argument, list_runtimes())


_RUNTIME_PRESETS: dict[str, tuple[str, tuple[str, str | None] | None]] = {
    "gemini": ("antigravity", ("gemini-4-neon-medium", None)),
    "neon": ("antigravity", ("gemini-4-neon-medium", None)),
    "argon": ("antigravity", ("gemini-4-argon-medium", None)),
    "claude": ("antigravity", ("claude-sonnet-5-5-medium", None)),
    "sonnet": ("antigravity", ("claude-sonnet-5-5-medium", None)),
    "antigravity-3p": ("antigravity", ("claude-sonnet-5-5-medium", None)),
    "opus": ("antigravity", ("claude-opus-5-5-medium", None)),
}


def _resolve_runtime_target(action: str) -> tuple[str, tuple[str, str | None] | None]:
    key = (action or "").strip().lower()
    if key in _RUNTIME_PRESETS:
        return _RUNTIME_PRESETS[key]
    return key, None


def _uses_native_audio(cs) -> bool:
    return (
        getattr(cs, "runtime_id", None) == "antigravity"
        and str(getattr(getattr(cs, "session", None), "model", "") or "").startswith("gemini-")
    )


async def h_gemini(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes
    await _switch_runtime_command(msg, "gemini", list_runtimes())


async def h_argon(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes
    await _switch_runtime_command(msg, "argon", list_runtimes())


async def h_claude(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes
    await _switch_runtime_command(msg, "claude", list_runtimes())


async def h_sonnet(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes
    await _switch_runtime_command(msg, "sonnet", list_runtimes())


async def h_opus(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes
    await _switch_runtime_command(msg, "opus", list_runtimes())


async def h_codex(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    from runtime_registry import list_runtimes
    await _switch_runtime_command(msg, "codex", list_runtimes())


def _runtime_keyboard_state(current) -> tuple[str, str]:
    if isinstance(current, str):
        return current, ""
    return (
        str(getattr(current, "runtime_id", "") or ""),
        str(getattr(getattr(current, "session", None), "model", "") or ""),
    )


def _runtime_keyboard(current) -> InlineKeyboardMarkup:
    runtime_id, model = _runtime_keyboard_state(current)

    def _active(action: str) -> bool:
        if action == "codex":
            return runtime_id == "codex"
        if runtime_id != "antigravity":
            return runtime_id == action
        if action == "argon":
            return model.startswith("gemini-4-argon-") or model in (
                "gemini-pro-orchestrator-g1-pro",
                "gemini-pro-orchestrator-argon",
            )
        if action == "opus":
            return model.startswith("claude-opus-")
        if action == "claude":
            return model.startswith("claude-sonnet-") or (
                model.startswith("claude-") and not model.startswith("claude-opus-")
            )
        if action == "gemini":
            return (not model) or (
                model.startswith("gemini-")
                and not model.startswith("gemini-4-argon-")
                and model not in ("gemini-pro-orchestrator-g1-pro", "gemini-pro-orchestrator-argon")
            )
        return False

    def button(action: str, title: str) -> InlineKeyboardButton:
        marker = "✅ " if _active(action) else ""
        return InlineKeyboardButton(
            text=f"{marker}{title}", callback_data=f"runtime:{action}"
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [button("gemini", "Gemini (Neon)"), button("argon", "Argon (Med)")],
        [button("claude", "Claude 3P (Sonnet 5.5)"), button("opus", "Opus 5.5 (3P)")],
        [button("codex", "Codex (Sol 6.2)"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data="runtime:refresh")],
    ])


async def _runtime_panel_text(source, cs) -> str:
    from runtime_registry import list_runtimes
    available = list_runtimes()
    other = next((r for r in available if r != cs.runtime_id), cs.runtime_id)
    return t(
        source,
        "runtime_status",
        current=cs.runtime_id,
        model=getattr(cs.session, "model", "?"),
        available=", ".join(available),
        other=other,
    )


async def _send_runtime_panel(msg: types.Message) -> None:
    cs = _registry.get(msg.chat.id)
    await _send_safe(
        msg,
        await _runtime_panel_text(msg, cs),
        reply_markup=_runtime_keyboard(cs),
    )


def _runtime_result_text(source, cs, target: str, available: list[str], result: dict) -> str:
    if result["ok"]:
        if result.get("handoff_status") == "same_runtime":
            lang = (getattr(getattr(source, "from_user", None), "language_code", None) or "").lower()
            handoff = (
                "Контекст текущей Antigravity-сессии сохранён."
                if lang.startswith("ru") or lang == ""
                else "Current Antigravity session context preserved."
            )
        elif result.get("handoff_status") == "unsupported":
            handoff = t(source, "runtime_handoff_unsupported")
        else:
            handoff = t(source, "runtime_handoff_ok" if result.get("handoff")
                        else "runtime_handoff_none")
        return t(
            source, "runtime_switched",
            previous=result["previous"],
            current=result["runtime"],
            model=result.get("model") or "?",
            handoff=handoff,
        )

    reason = result.get("reason")
    if reason == "same":
        return t(source, "runtime_same", current=cs.runtime_id)
    if reason == "unknown":
        return t(source, "runtime_unknown", runtime=target,
                 available=", ".join(available))
    if reason == "busy":
        return t(source, "runtime_busy")
    return t(
        source, "runtime_failed",
        runtime=target,
        error=str(result.get("error") or reason)[:300],
        fallback=result.get("fallback") or cs.runtime_id,
    )


async def _switch_runtime_command(
    msg: types.Message,
    target: str,
    available: list[str],
) -> None:
    runtime_target, selection = _resolve_runtime_target(target)
    if runtime_target == "antigravity" and not _interactive_sender(msg):
        return
    cs = _registry.get(msg.chat.id)
    result = (
        await cs.switch_runtime(runtime_target, selection=selection)
        if selection is not None
        else await cs.switch_runtime(runtime_target)
    )
    await _send_safe(
        msg,
        _runtime_result_text(msg, cs, runtime_target, available, result),
        reply_markup=_runtime_keyboard(cs),
    )


async def h_runtime_callback(query: types.CallbackQuery):
    """Acknowledge Telegram immediately, then refresh or switch the chat."""
    if not allowed(query.from_user.id):
        await query.answer(t(query, "no_access", uid=query.from_user.id), show_alert=True)
        return
    await query.answer()
    if query.message is None or not query.data:
        return

    from runtime_registry import list_runtimes

    cs = _registry.get(query.message.chat.id)
    action = query.data.removeprefix("runtime:")
    prefix = ""
    if action != "refresh":
        runtime_target, selection = _resolve_runtime_target(action)
        if runtime_target == "antigravity" and not _interactive_sender(query):
            return
        available = list_runtimes()
        result = (
            await cs.switch_runtime(runtime_target, selection=selection)
            if selection is not None
            else await cs.switch_runtime(runtime_target)
        )
        prefix = _runtime_result_text(query, cs, runtime_target, available, result) + "\n\n"

    text = prefix + await _runtime_panel_text(query, cs)
    try:
        await query.message.edit_text(
            text,
            reply_markup=_runtime_keyboard(cs),
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning(f"runtime callback edit failed: {exc}")


def _model_keyboard(cs):
    rows = []
    active = (cs.session.model, getattr(cs.session, "reasoning_effort", None))
    for runtime_id, families in MODELS.items():
        for index, (title, _, efforts) in enumerate(families):
            buttons = []
            for effort in efforts:
                selected = cs.runtime_id == runtime_id and resolve_choice(runtime_id, index, effort) == active
                label = f"{title} · {effort}" if effort else title
                buttons.append(InlineKeyboardButton(text=("✓ " if selected else "") + label,
                                                    callback_data=f"model:{runtime_id}:{index}:{effort}"))
            rows.extend(buttons[i:i + 3] for i in range(0, len(buttons), 3))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _model_panel(cs):
    effort = getattr(cs.session, "reasoning_effort", None)
    return f"/model · {cs.session.model}" + (f" · {effort}" if effort else "")


async def h_model(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cs = _registry.get(msg.chat.id)
    await _send_safe(msg, _model_panel(cs), reply_markup=_model_keyboard(cs), parse_mode=None)


async def h_model_callback(query: types.CallbackQuery):
    if not allowed(query.from_user.id) or getattr(query.from_user, "is_bot", False):
        await query.answer(t(query, "no_access", uid=query.from_user.id), show_alert=True)
        return
    await query.answer()
    if not query.message or not query.data:
        return
    try:
        _, runtime, index, effort = query.data.split(":", 3)
        if not index.isdigit():
            return
        selection = resolve_choice(runtime, int(index), effort)
    except (IndexError, KeyError, ValueError):
        return
    if runtime == "antigravity" and query.from_user.id not in ALLOWED:
        return
    cs = _registry.get(query.message.chat.id)
    result = await cs.switch_runtime(runtime, selection=selection)
    text = _runtime_result_text(query, cs, runtime, list(MODELS), result)
    try:
        await query.message.edit_text(text + "\n\n" + _model_panel(cs), reply_markup=_model_keyboard(cs))
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Model picker edit failed: %s", type(exc).__name__)


async def h_ping(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    sid = _registry.get(msg.chat.id).session.session_id
    await _send_safe(msg, t(msg, "ping", session=sid or "none"))


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




async def h_reload(msg: types.Message):
    """Respawn the CLI so a freshly edited .mcp.json takes effect."""
    if not allowed(msg.from_user.id):
        return
    result = await _registry.get(msg.chat.id).reload_cli()
    if not result["ok"]:
        if result["reason"] == "busy":
            await _send_safe(msg, t(msg, "reload_busy"))
        else:
            await _send_safe(
                msg,
                t(msg, "reload_failed", error=str(result.get("error") or result["reason"])[:300]),
            )
        return

    changes = []
    if result["added"]:
        changes.append(t(msg, "reload_added", added=", ".join(result["added"])))
    if result["removed"]:
        changes.append(t(msg, "reload_removed", removed=", ".join(result["removed"])))
    await _send_safe(msg, t(
        msg, "reload_done",
        session=result["session"] or "none",
        count=len(result["servers"]),
        servers=", ".join(result["servers"]),
        changes="\n".join(changes) or t(msg, "reload_unchanged"),
    ))


async def h_restart(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    if not is_owner(getattr(msg.from_user, "id", None)):
        await _send_safe(msg, "⛔ Команда /restart доступна только владельцу")
        return
    await _send_safe(msg, t(msg, "restarting"))
    p = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", BOT_SERVICE_UNIT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await p.communicate()


async def h_orch(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    if not is_owner(getattr(msg.from_user, "id", None)):
        await _send_safe(msg, "⛔ Команда /orch доступна только владельцу")
        return
    from orch_ops import handle_orch_command
    out = await handle_orch_command(msg.text or "/orch status")
    await _send_safe(msg, out)


async def h_stop(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    cid = msg.chat.id
    cs = _registry.get(cid)
    # The CLI's interrupt also stops its background agents (V-43 research §5).
    agents = cs.background_agents_open()
    stopped = await cs.request_stop()
    if stopped:
        await _send_safe(
            msg, t(msg, "stop_with_background", n=agents) if agents else "Stopping..."
        )
    else:
        await _send_safe(msg, "Nothing to stop.")


# --- Media handlers ---

def _native_voice_tag(path, duration, source="voice", video=None):
    if source == "video_note":
        dur = f" ({duration}s)" if duration else ""
        return (f"[video_note: {video or path}{dur} — кружочек: "
                "посмотри и прослушай исходный .mp4 нативно через read_file и ответь по сути]")
    if source == "video":
        dur = f" ({duration}s)" if duration else ""
        return (f"[video: {video or path}{dur} — видео: "
                "посмотри и прослушай .mp4 нативно через read_file]")
    if source == "audio":
        return f"[audio: {path} ({duration}s) — прослушай аудиофайл через read_file]"
    return (f"[voice: {path} ({duration}s) — голосовое сообщение (без Deepgram): "
            "прослушай сам напрямую через read_file и ответь по сути]")


async def _enqueue_native_voice(msg, media, path, duration, *, convert=False, video_note=False, source="voice"):
    from media import native_ogg

    try:
        ogg = await native_ogg(path, convert=convert)
    except (OSError, RuntimeError, TimeoutError):
        await _send_safe(msg, "Не удалось подготовить голосовое сообщение в Ogg.")
        state, generation, media_generation = media
        await state.media_finished(None, generation, media_generation)
        return
    tag = _native_voice_tag(ogg, duration, "video_note" if video_note else source, path)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    await enqueue(msg, tag + caption, media=media, audio_paths=(ogg,))


async def h_voice(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    media = await _start_media(msg)
    if media is None:
        return
    cs, gen, media_gen = media
    native = _uses_native_audio(cs)
    path = await download_file(msg.voice.file_id,
                               _media_name("voice", ".ogg" if native else ".oga", msg),
                               msg.voice.file_unique_id)
    if not path:
        await enqueue(msg, "[voice: файл слишком большой]", media=media)
        return
    if native:
        await _enqueue_native_voice(msg, media, path, msg.voice.duration)
        return
    from config import DEEPGRAM as _DG
    if not _DG:
        await enqueue(msg, f"[voice: {path}]", media=media)
        return

    await _bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        text, err = await transcribe(path, msg.voice.file_unique_id or "")
    except Exception as e:
        text, err = "", str(e)

    if not text:
        err_msg = t(msg, "voice_fail")
        if err:
            err_msg += f" ({err})"
        await _send_safe(msg, err_msg)
        full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[voice: {path}] (transcription failed: {err or 'unknown'})"
        fallback_entry = PendingEntry(
            prompt=full_prompt, message_id=msg.message_id, message=msg,
            source="user", reply_target=chat_id, interactive_user=_interactive_sender(msg),
        )
        await cs.media_finished(fallback_entry, gen, media_gen)
        return

    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[voice: {path} | {text}]"
    entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
        interactive_user=_interactive_sender(msg),
    )
    await cs.media_finished(entry, gen, media_gen)


@media_group_handler
async def h_media_album(messages: list[types.Message]):
    if not allowed(messages[0].from_user.id):
        return await _deny_once(messages[0])
    media = await _start_media(messages[0])
    if media is None:
        return
    from media import native_ogg

    parts = []
    audio_paths = []
    native = _uses_native_audio(media[0])
    for m in messages:
        if m.photo:
            path = await download_file(m.photo[-1].file_id, _media_name("photo", ".jpg", m), m.photo[-1].file_unique_id)
            tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
            parts.append(tag)
        elif m.video:
            path = await download_file(m.video.file_id, m.video.file_name or _media_name("video", ".mp4", m), m.video.file_unique_id)
            if path and native:
                tag = _native_voice_tag(path, getattr(m.video, "duration", None), "video", path)
            else:
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
            if path and native:
                try:
                    ogg = await native_ogg(path, convert=True)
                except (OSError, RuntimeError, TimeoutError):
                    tag = "[audio: не удалось подготовить файл в Ogg]"
                else:
                    audio_paths.append(ogg)
                    tag = _native_voice_tag(ogg, m.audio.duration, "audio")
            else:
                tag = f"[audio: {path} ({name})]" if path else f"[audio: файл слишком большой ({name})]"
            parts.append(tag)
    caption = ""
    for m in messages:
        if m.caption:
            caption = f"\n{extract_caption_with_urls(m)}"
            break
    fwd = forward_meta(messages[0])
    media_block = "\n".join(parts)
    bare_photo = not caption and all(m.photo for m in messages)
    await enqueue(
        messages[0], f"{fwd}{media_block}{caption}", media=media, bare_photo=bare_photo,
        audio_paths=tuple(audio_paths),
    )


async def h_photo(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    media = await _start_media(msg)
    if media is None:
        return
    path = await download_file(msg.photo[-1].file_id, _media_name("photo", ".jpg", msg), msg.photo[-1].file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[photo: {path}]" if path else "[photo: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}", media=media, bare_photo=not msg.caption)


async def h_video_note(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    chat_id = msg.chat.id
    media = await _start_media(msg)
    if media is None:
        return
    cs, gen, media_gen = media
    native = _uses_native_audio(cs)
    path = await download_file(msg.video_note.file_id, _media_name("videonote", ".mp4", msg), msg.video_note.file_unique_id)
    if not path:
        await enqueue(msg, "[video_note: файл слишком большой]", media=media)
        return
    if native:
        tag = _native_voice_tag(path, msg.video_note.duration, "video_note", path)
        await enqueue(msg, tag, media=media)
        return
    from config import DEEPGRAM as _DG
    if not _DG:
        await enqueue(msg, f"[video_note: {path}]", media=media)
        return

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
                interactive_user=_interactive_sender(msg),
            )
            await cs.media_finished(entry, gen, media_gen)
            return
    full_prompt = f"{user_prefix(msg)}: {forward_meta(msg)}{reply_meta(msg)}[video_note: {path}]"
    fallback_entry = PendingEntry(
        prompt=full_prompt,
        message_id=msg.message_id,
        message=msg,
        source="user",
        reply_target=chat_id,
        interactive_user=_interactive_sender(msg),
    )
    await cs.media_finished(fallback_entry, gen, media_gen)


async def h_document(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    media = await _start_media(msg)
    if media is None:
        return
    doc = msg.document
    ext = os.path.splitext(doc.file_name or "file")[1] or ".bin"
    path = await download_file(doc.file_id, doc.file_name or _media_name("doc", ext, msg), doc.file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    tag = f"[document: {path} ({doc.file_name})]" if path else f"[document: файл слишком большой ({doc.file_name})]"
    await enqueue(msg, f"{tag}{caption}", media=media)


async def h_sticker(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    emoji = msg.sticker.emoji or "?"
    await enqueue(msg, f"[sticker: {emoji}]")


async def h_video(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    media = await _start_media(msg)
    if media is None:
        return
    path = await download_file(msg.video.file_id, msg.video.file_name or _media_name("video", ".mp4", msg), msg.video.file_unique_id)
    caption = f"\n{extract_caption_with_urls(msg)}" if msg.caption else ""
    if path and _uses_native_audio(media[0]):
        tag = _native_voice_tag(path, getattr(msg.video, "duration", None), "video", path)
    else:
        tag = f"[video: {path}]" if path else "[video: файл слишком большой]"
    await enqueue(msg, f"{tag}{caption}", media=media)


async def h_audio(msg: types.Message):
    if not allowed(msg.from_user.id):
        return
    media = await _start_media(msg)
    if media is None:
        return
    ext = os.path.splitext(msg.audio.file_name or "audio.mp3")[1] or ".mp3"
    name = msg.audio.file_name or _media_name("audio", ext, msg)
    path = await download_file(msg.audio.file_id, name, msg.audio.file_unique_id)
    if path and _uses_native_audio(media[0]):
        await _enqueue_native_voice(msg, media, path, msg.audio.duration, convert=True, source="audio")
        return
    tag = f"[audio: {path} ({name})]" if path else f"[audio: файл слишком большой ({name})]"
    await enqueue(msg, tag, media=media)


async def h_text(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    await enqueue(msg, extract_text_with_urls(msg))


async def h_fallback(msg: types.Message):
    if not allowed(msg.from_user.id):
        return await _deny_once(msg)
    content_type = msg.content_type or "unknown"
    rich_message = getattr(msg, "rich_message", None)
    if content_type == ContentType.RICH_MESSAGE and rich_message is not None:
        text = f"[rich_message] {rich_message.model_dump_json(exclude_none=True)}"
    else:
        text = msg.text or msg.caption or (
            f"[unhandled message: {getattr(content_type, 'value', content_type)}]"
        )
    logger.warning(f"Chat {msg.chat.id}: unhandled message type={content_type}, text={text[:100]}")
    await enqueue(msg, text)


# --- Command lists ---

COMMANDS_RU = [
    BotCommand(command="start", description="Статус бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="status", description="Подробный статус"),
    BotCommand(command="limits", description="Лимиты Claude, Codex, Spark и Grok"),
    BotCommand(command="clear", description="Сбросить сессию"),
    BotCommand(command="compact", description="Сжать контекст (сохранить краткую выжимку)"),
    BotCommand(command="model", description="Модель / Model"),
    BotCommand(command="runtime", description="Рантайм и пулы моделей"),
    BotCommand(command="gemini", description="Antigravity Gemini (Neon Medium)"),
    BotCommand(command="argon", description="Antigravity Argon (Medium)"),
    BotCommand(command="claude", description="Antigravity Claude 3P (Sonnet 5.5)"),
    BotCommand(command="sonnet", description="Antigravity Sonnet 5.5 Medium (3P)"),
    BotCommand(command="opus", description="Antigravity Opus 5.5 Medium (3P)"),
    BotCommand(command="codex", description="Переключиться на Codex (Sol 6.2)"),
    BotCommand(command="ping", description="Проверить сессию"),
    BotCommand(command="debounce", description="Задержка склейки сообщений"),
    BotCommand(command="debug", description="Вкл/выкл debug логи"),
    BotCommand(command="reload", description="Перезапустить CLI: новые MCP-тулы, переписка сохраняется"),
    BotCommand(command="orch", description="Пульт Orchestra (status/logs/restart/rollback)"),
    BotCommand(command="restart", description="Перезапустить бота"),
]

COMMANDS_EN = [
    BotCommand(command="start", description="Bot status"),
    BotCommand(command="help", description="Command reference"),
    BotCommand(command="status", description="Detailed status"),
    BotCommand(command="limits", description="Claude, Codex, Spark and Grok limits"),
    BotCommand(command="clear", description="Clear session"),
    BotCommand(command="compact", description="Compact context (keep a summary)"),
    BotCommand(command="model", description="Модель / Model"),
    BotCommand(command="runtime", description="Runtime and model pools"),
    BotCommand(command="gemini", description="Antigravity Gemini (Neon Medium)"),
    BotCommand(command="argon", description="Antigravity Argon (Medium)"),
    BotCommand(command="claude", description="Antigravity Claude 3P (Sonnet 5.5)"),
    BotCommand(command="sonnet", description="Antigravity Sonnet 5.5 Medium (3P)"),
    BotCommand(command="opus", description="Antigravity Opus 5.5 Medium (3P)"),
    BotCommand(command="codex", description="Switch to Codex (Sol 6.2)"),
    BotCommand(command="ping", description="Check session"),
    BotCommand(command="debounce", description="Message batching delay"),
    BotCommand(command="debug", description="Toggle debug logs"),
    BotCommand(command="reload", description="Restart CLI: new MCP tools, conversation kept"),
    BotCommand(command="orch", description="Orchestra control panel"),
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
    dp.message.register(h_limits, Command("limits"))
    dp.message.register(h_clear, Command("clear"))
    dp.message.register(h_compact, Command("compact"))
    dp.message.register(h_runtime, Command("runtime"))
    dp.message.register(h_model, Command("model"))
    dp.callback_query.register(h_model_callback, F.data.startswith("model:"))
    dp.message.register(h_gemini, Command("gemini"))
    dp.message.register(h_argon, Command("argon"))
    dp.message.register(h_claude, Command("claude"))
    dp.message.register(h_sonnet, Command("sonnet"))
    dp.message.register(h_opus, Command("opus"))
    dp.message.register(h_codex, Command("codex"))
    dp.callback_query.register(h_runtime_callback, F.data.startswith("runtime:"))
    dp.message.register(h_ping, Command("ping"))
    dp.message.register(h_debounce, Command("debounce"))
    dp.message.register(h_debug, Command("debug"))
    dp.message.register(h_reload, Command("reload"))
    dp.message.register(h_orch, Command("orch"))
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
