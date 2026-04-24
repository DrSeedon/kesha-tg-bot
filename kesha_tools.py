"""Kesha self-configuration tools — injected as SDK MCP server."""

import asyncio
import contextvars
import logging
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server

import reminders as _rem

logger = logging.getLogger("kesha.tools")

_bot_ref = None
_current_chat_id: contextvars.ContextVar[int | None] = contextvars.ContextVar('_current_chat_id', default=None)


def set_bot_ref(bot_module):
    global _bot_ref
    _bot_ref = bot_module


def set_current_chat(chat_id: int):
    """Set the active chat_id for MCP tools routing. Called from bot.py before _ask."""
    _current_chat_id.set(chat_id)


def get_current_chat() -> int | None:
    """Get the chat_id that triggered the current Claude session."""
    return _current_chat_id.get(None)


def _resolve_chat() -> int | None:
    return get_current_chat() or (next(iter(_bot_ref.ALLOWED), None) if _bot_ref else None)


ALLOWED_MODELS = {
    "opus": "claude-opus-4-6",
    "opus 1m": "claude-opus-4-6",
    "opus 200k": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "sonnet 1m": "claude-sonnet-4-6",
    "sonnet 200k": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


@tool("set_model", "Change Claude model. Options: opus, sonnet, haiku. Default 1M context. Add '200k' for standard context (e.g. 'sonnet 200k')", {"model": str})
async def set_model(args):
    name = args["model"].strip().lower()
    use_200k = "200k" in name
    name = name.replace("200k", "").replace("1m", "").strip()
    model_id = ALLOWED_MODELS.get(name)
    if not model_id:
        return {"content": [{"type": "text", "text": f"Unknown model '{name}'. Available: opus, sonnet, haiku (+ '200k' for standard context)"}], "is_error": True}
    claude = _bot_ref.get_session(_resolve_chat())
    claude.use_1m = not use_200k
    await claude.set_model_live(model_id)
    ctx = "200K" if use_200k else "1M"
    logger.info(f"Model changed to {model_id} ({ctx})")
    return {"content": [{"type": "text", "text": f"Model changed to {model_id} ({ctx} context)"}]}


@tool("set_debounce", "Change message debounce delay in seconds (0-30)", {"seconds": int})
async def set_debounce(args):
    sec = args["seconds"]
    if not 0 <= sec <= 30:
        return {"content": [{"type": "text", "text": "Debounce must be 0-30 seconds"}], "is_error": True}
    chat_id = _resolve_chat()
    if chat_id and _bot_ref and hasattr(_bot_ref, 'registry') and _bot_ref.registry:
        await _bot_ref.registry.get(chat_id).set_debounce(sec)
        _bot_ref.registry._debounce_sec = sec
    import config as _cfg
    _cfg.DEBOUNCE_SEC = sec
    logger.info(f"Debounce changed to {sec}s")
    return {"content": [{"type": "text", "text": f"Debounce changed to {sec}s"}]}


@tool("toggle_debug", "Toggle debug logging on/off", {})
async def toggle_debug(args):
    import config as _cfg
    _cfg.DEBUG = not _cfg.DEBUG
    _cfg.logger.setLevel(logging.DEBUG if _cfg.DEBUG else logging.INFO)
    state = "on" if _cfg.DEBUG else "off"
    logger.info(f"Debug toggled {state}")
    return {"content": [{"type": "text", "text": f"Debug is now {state}"}]}


@tool("get_bot_status", "Get current bot configuration and status", {})
async def get_bot_status(args):
    import config as _cfg
    import media as _media
    chat_id = _resolve_chat()
    c = _bot_ref.get_session(chat_id)
    rl = c.rate_limit
    if rl:
        util = rl.get('utilization')
        util_str = f" {int(util*100)}%" if util is not None else ""
        rl_str = f"{rl.get('status', '?')} ({rl.get('type', '?')}){util_str}"
    else:
        rl_str = "unknown"
    dur = f"{c.last_duration_ms/1000:.1f}s" if c.last_duration_ms else "n/a"
    ctx = await c.get_context_usage()
    ctx_str = f"{ctx['percentage']:.0f}% ({ctx['totalTokens']}/{ctx['maxTokens']})" if ctx else "n/a"
    status = (
        f"Model: {c.model}\n"
        f"Session: {c.session_id or 'none'}\n"
        f"Debounce: {_bot_ref.registry.get(chat_id).debounce_sec if _bot_ref and hasattr(_bot_ref, 'registry') and _bot_ref.registry and chat_id else _cfg.DEBOUNCE_SEC}s\n"
        f"Debug: {'on' if _cfg.DEBUG else 'off'}\n"
        f"CWD: {_cfg.WORK_DIR}\n"
        f"Rate limit: {rl_str}\n"
        f"Session cost: ${c.total_cost_usd:.4f}\n"
        f"Context: {ctx_str}\n"
        f"Last response: {dur}, {c.last_num_turns} turns, stop={c.last_stop_reason}\n"
        f"Media files: {_media.media_count()}\n"
        f"Log size: {_media.log_size()}"
    )
    return {"content": [{"type": "text", "text": status}]}


@tool("restart_bot", "Restart the bot service (applies code changes)", {})
async def restart_bot(args):
    logger.info("Bot restart requested via tool")
    greet_flag = Path(__file__).parent / "storage" / "greet_on_restart"
    greet_flag.parent.mkdir(parents=True, exist_ok=True)
    with open(greet_flag, "w") as f:
        f.write("1")
        f.flush()
        import os as _os
        _os.fsync(f.fileno())
    asyncio.get_event_loop().call_later(1.0, lambda: asyncio.ensure_future(_do_restart()))
    return {"content": [{"type": "text", "text": "Bot restarting in 1s..."}]}

async def _do_restart():
    await asyncio.create_subprocess_exec("sudo", "systemctl", "restart", "kesha-bot")


@tool("send_photo", "Send a photo to the user in Telegram", {"path": str, "caption": str})
async def send_photo(args):
    path = args["path"]
    caption = args.get("caption", "")
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    p = Path(path)
    if not p.exists():
        return {"content": [{"type": "text", "text": f"File not found: {path}"}], "is_error": True}
    try:
        from aiogram.types import FSInputFile
        photo = FSInputFile(str(p))
        await _bot_ref.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption or None)
        logger.info(f"Sent photo {path} to {chat_id}")
        return {"content": [{"type": "text", "text": f"Photo sent: {p.name}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to send photo: {e}"}], "is_error": True}


@tool("send_file", "Send any file/document to the user in Telegram", {"path": str, "caption": str})
async def send_file(args):
    path = args["path"]
    caption = args.get("caption", "")
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    p = Path(path)
    if not p.exists():
        return {"content": [{"type": "text", "text": f"File not found: {path}"}], "is_error": True}
    try:
        from aiogram.types import FSInputFile
        doc = FSInputFile(str(p))
        await _bot_ref.bot.send_document(chat_id=chat_id, document=doc, caption=caption or None)
        logger.info(f"Sent file {path} to {chat_id}")
        return {"content": [{"type": "text", "text": f"File sent: {p.name}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to send file: {e}"}], "is_error": True}


@tool(
    "create_reminder",
    "Create reminder. when_iso: ISO datetime in UTC (e.g. '2026-04-11T09:00:00+00:00'). "
    "type: 'plain' (raw text at time, no LLM), 'urgent_llm' (Claude formulates and sends at time), "
    "'lazy_llm' (silent until user writes — then injected into next prompt). "
    "repeat_interval optional: '30m'/'2h'/'1d'/'1w'/'3mo'. "
    "repeat_at_time optional: 'HH:MM' (Krsk +07) to align repeats to specific time of day.",
    {"text": str, "when_iso": str, "type": str, "repeat_interval": str, "repeat_at_time": str},
)
async def create_reminder(args):
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    try:
        text = args["text"]
        when_iso = args["when_iso"]
        type_ = args["type"]
        rep_int = args.get("repeat_interval") or None
        rep_at = args.get("repeat_at_time") or None
        due = _rem.parse_iso(when_iso)
        rid = _rem.get_db().create(chat_id, text, due, type_, rep_int, rep_at)
        local = due.astimezone(_rem.KRSK_TZ).strftime("%Y-%m-%d %H:%M %z")
        rep_str = f", repeat={rep_int}" + (f"@{rep_at}" if rep_at else "") if rep_int else ""
        logger.info(f"Reminder #{rid} created: {type_} at {when_iso}{rep_str}: {text[:60]}")
        return {"content": [{"type": "text", "text": f"Reminder #{rid} saved: [{type_}] {local}{rep_str} — {text}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to create reminder: {e}"}], "is_error": True}


@tool("list_reminders", "List reminders for current chat. include_fired=true to also show delivered/past ones.",
      {"include_fired": bool})
async def list_reminders(args):
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    include = bool(args.get("include_fired", False))
    rows = _rem.get_db().list_for(chat_id, include_fired=include)
    if not rows:
        return {"content": [{"type": "text", "text": "No reminders" + (" (incl. fired)" if include else " (pending)")}]}
    lines = [_rem.format_reminder_line(r) for r in rows]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool("cancel_reminder", "Cancel/delete reminder by id", {"id": int})
async def cancel_reminder(args):
    rid = args["id"]
    db = _rem.get_db()
    row = db.get(rid)
    if not row:
        return {"content": [{"type": "text", "text": f"Reminder #{rid} not found"}], "is_error": True}
    chat_id = _resolve_chat()
    if chat_id and row["chat_id"] != chat_id:
        return {"content": [{"type": "text", "text": f"Reminder #{rid} belongs to another chat"}], "is_error": True}
    db.cancel(rid)
    logger.info(f"Reminder #{rid} cancelled")
    return {"content": [{"type": "text", "text": f"Reminder #{rid} cancelled"}]}


@tool(
    "update_reminder",
    "Update reminder fields by id. Pass only fields to change. "
    "when_iso: new ISO UTC datetime. type/text/repeat_interval/repeat_at_time as in create_reminder.",
    {"id": int, "text": str, "when_iso": str, "type": str, "repeat_interval": str, "repeat_at_time": str},
)
async def update_reminder(args):
    rid = args["id"]
    db = _rem.get_db()
    existing = db.get(rid)
    if not existing:
        return {"content": [{"type": "text", "text": f"Reminder #{rid} not found"}], "is_error": True}
    chat_id = _resolve_chat()
    if chat_id and existing["chat_id"] != chat_id:
        return {"content": [{"type": "text", "text": f"Reminder #{rid} belongs to another chat"}], "is_error": True}
    fields = {}
    if "text" in args and args["text"]:
        fields["text"] = args["text"]
    if "type" in args and args["type"]:
        fields["type"] = args["type"]
    if "when_iso" in args and args["when_iso"]:
        fields["due_at"] = _rem.parse_iso(args["when_iso"])
        fields["fired_at"] = None
        fields["delivered"] = 0
    if "repeat_interval" in args:
        fields["repeat_interval"] = args["repeat_interval"] or None
    if "repeat_at_time" in args:
        fields["repeat_at_time"] = args["repeat_at_time"] or None
    if not fields:
        return {"content": [{"type": "text", "text": "No fields to update"}], "is_error": True}
    try:
        db.update(rid, **fields)
        logger.info(f"Reminder #{rid} updated: {list(fields.keys())}")
        return {"content": [{"type": "text", "text": f"Reminder #{rid} updated"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Update failed: {e}"}], "is_error": True}


@tool("send_video", "Send a video to the user in Telegram (with player/preview)", {"path": str, "caption": str})
async def send_video(args):
    path = args["path"]
    caption = args.get("caption", "")
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    p = Path(path)
    if not p.exists():
        return {"content": [{"type": "text", "text": f"File not found: {path}"}], "is_error": True}
    try:
        from aiogram.types import FSInputFile
        video = FSInputFile(str(p))
        await _bot_ref.bot.send_video(chat_id=chat_id, video=video, caption=caption or None)
        logger.info(f"Sent video {path} to {chat_id}")
        return {"content": [{"type": "text", "text": f"Video sent: {p.name}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to send video: {e}"}], "is_error": True}


@tool("send_audio", "Send an audio file to the user in Telegram (with player)", {"path": str, "caption": str})
async def send_audio(args):
    path = args["path"]
    caption = args.get("caption", "")
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    p = Path(path)
    if not p.exists():
        return {"content": [{"type": "text", "text": f"File not found: {path}"}], "is_error": True}
    try:
        from aiogram.types import FSInputFile
        audio = FSInputFile(str(p))
        await _bot_ref.bot.send_audio(chat_id=chat_id, audio=audio, caption=caption or None)
        logger.info(f"Sent audio {path} to {chat_id}")
        return {"content": [{"type": "text", "text": f"Audio sent: {p.name}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to send audio: {e}"}], "is_error": True}


@tool("send_voice", "Send a voice message to the user in Telegram", {"path": str})
async def send_voice(args):
    path = args["path"]
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    p = Path(path)
    if not p.exists():
        return {"content": [{"type": "text", "text": f"File not found: {path}"}], "is_error": True}
    try:
        from aiogram.types import FSInputFile
        voice = FSInputFile(str(p))
        await _bot_ref.bot.send_voice(chat_id=chat_id, voice=voice)
        logger.info(f"Sent voice {path} to {chat_id}")
        return {"content": [{"type": "text", "text": f"Voice sent: {p.name}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Failed to send voice: {e}"}], "is_error": True}


@tool("react", "Set emoji reaction on specific messages. REQUIRED: pass 'reactions' array with msg_id and emoji: [{\"msg_id\": 123, \"emoji\": \"😂\"}]. msg_id from [msg_id=X] tags. If reactions is empty or missing msg_id — returns error, does NOT react to all.", {
    "emoji": str,
    "reactions": list,
})
async def react(args):
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    try:
        from aiogram.types import ReactionTypeEmoji

        reaction_list = args.get("reactions")
        if reaction_list:
            pairs = [r for r in reaction_list if r.get("msg_id")]
        else:
            pairs = []
        if not pairs:
            return {"content": [{"type": "text", "text": "No reactions: provide reactions array with msg_id. Empty array = no action."}], "is_error": True}

        reacted = []
        for p in pairs:
            mid = p.get("msg_id")
            em = p.get("emoji", "👍")
            if not mid:
                continue
            try:
                await _bot_ref.bot.set_message_reaction(
                    chat_id=chat_id, message_id=mid,
                    reaction=[ReactionTypeEmoji(emoji=em)]
                )
                reacted.append(f"{em}→{mid}")
            except Exception as e:
                logger.debug(f"React failed for msg {mid} {em}: {e}")

        logger.info(f"Reacted to {len(reacted)} messages: {reacted}")
        return {"content": [{"type": "text", "text": f"Reacted: {', '.join(reacted)}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"React failed: {e}"}], "is_error": True}


@tool(
    "compact_context",
    "Compact/summarize the current conversation to free up context window. Use this when you notice context usage is getting high (>80%). You'll summarize the conversation yourself, then the session resets and continues with the summary as foundation. User sees a notification with before→after percentages.",
    {},
)
async def compact_context(args):
    chat_id = _resolve_chat()
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    cs = _bot_ref.registry.get(chat_id)
    if cs.is_busy:
        return {"content": [{"type": "text", "text": "Cannot compact while processing — use /compact command between messages instead"}], "is_error": True}
    import compact as _compact
    try:
        async def _notify(text):
            await _bot_ref.bot.send_message(chat_id, text)
        claude = _bot_ref.get_session(chat_id)
        result = await _compact.compact_session(claude, notify=_notify)
        if result.get("ok"):
            msg = f"Compacted {result['before_pct']:.0f}% → {result['after_pct']:.0f}%, summary={result['summary_chars']} chars"
            logger.info(msg)
            return {"content": [{"type": "text", "text": msg}]}
        else:
            return {"content": [{"type": "text", "text": f"Compact failed: {result.get('error')}"}], "is_error": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Compact error: {e}"}], "is_error": True}


kesha_server = create_sdk_mcp_server(
    name="kesha",
    tools=[set_model, set_debounce, toggle_debug, get_bot_status, restart_bot,
           send_photo, send_file, send_video, send_audio, send_voice, react,
           create_reminder, list_reminders, cancel_reminder, update_reminder,
           compact_context],
)
