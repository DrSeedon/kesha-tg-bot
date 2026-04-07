"""Kesha self-configuration tools — injected as SDK MCP server."""

import asyncio
import logging
from pathlib import Path

from claude_agent_sdk import tool, create_sdk_mcp_server

logger = logging.getLogger("kesha.tools")

_bot_ref = None


def set_bot_ref(bot_module):
    global _bot_ref
    _bot_ref = bot_module


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
    _bot_ref.claude.use_1m = not use_200k
    await _bot_ref.claude.set_model_live(model_id)
    ctx = "200K" if use_200k else "1M"
    logger.info(f"Model changed to {model_id} ({ctx})")
    return {"content": [{"type": "text", "text": f"Model changed to {model_id} ({ctx} context)"}]}


@tool("set_debounce", "Change message debounce delay in seconds (0-30)", {"seconds": int})
async def set_debounce(args):
    sec = args["seconds"]
    if not 0 <= sec <= 30:
        return {"content": [{"type": "text", "text": "Debounce must be 0-30 seconds"}], "is_error": True}
    _bot_ref.DEBOUNCE_SEC = sec
    logger.info(f"Debounce changed to {sec}s")
    return {"content": [{"type": "text", "text": f"Debounce changed to {sec}s"}]}


@tool("toggle_debug", "Toggle debug logging on/off", {})
async def toggle_debug(args):
    _bot_ref.DEBUG = not _bot_ref.DEBUG
    _bot_ref.logger.setLevel(logging.DEBUG if _bot_ref.DEBUG else logging.INFO)
    state = "on" if _bot_ref.DEBUG else "off"
    logger.info(f"Debug toggled {state}")
    return {"content": [{"type": "text", "text": f"Debug is now {state}"}]}


@tool("get_bot_status", "Get current bot configuration and status", {})
async def get_bot_status(args):
    c = _bot_ref.claude
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
        f"Debounce: {_bot_ref.DEBOUNCE_SEC}s\n"
        f"Debug: {'on' if _bot_ref.DEBUG else 'off'}\n"
        f"CWD: {_bot_ref.WORK_DIR}\n"
        f"Rate limit: {rl_str}\n"
        f"Session cost: ${c.total_cost_usd:.4f}\n"
        f"Context: {ctx_str}\n"
        f"Last response: {dur}, {c.last_num_turns} turns, stop={c.last_stop_reason}\n"
        f"Media files: {_bot_ref.media_count()}\n"
        f"Log size: {_bot_ref.log_size()}"
    )
    return {"content": [{"type": "text", "text": status}]}


@tool("restart_bot", "Restart the bot service (applies code changes)", {})
async def restart_bot(args):
    logger.info("Bot restart requested via tool")
    p = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "kesha-bot",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await p.communicate()
    if p.returncode != 0:
        return {"content": [{"type": "text", "text": f"Restart failed: {err.decode()}"}], "is_error": True}
    return {"content": [{"type": "text", "text": "Bot restarting..."}]}


@tool("send_photo", "Send a photo to the user in Telegram", {"path": str, "caption": str})
async def send_photo(args):
    path = args["path"]
    caption = args.get("caption", "")
    chat_id = next(iter(_bot_ref.ALLOWED), None)
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
    chat_id = next(iter(_bot_ref.ALLOWED), None)
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


@tool("schedule_message", "Schedule a message to be sent after a delay", {"message": str, "delay_seconds": int})
async def schedule_message(args):
    message = args["message"]
    delay = args["delay_seconds"]
    chat_id = next(iter(_bot_ref.ALLOWED), None)
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    if delay < 1 or delay > 86400:
        return {"content": [{"type": "text", "text": "Delay must be 1-86400 seconds (max 24h)"}], "is_error": True}

    async def _send_later():
        await asyncio.sleep(delay)
        try:
            await _bot_ref.bot.send_message(chat_id=chat_id, text=message)
            logger.info(f"Scheduled message sent to {chat_id} after {delay}s")
        except Exception as e:
            logger.error(f"Scheduled message failed: {e}")

    asyncio.create_task(_send_later())
    mins = delay // 60
    secs = delay % 60
    time_str = f"{mins}м {secs}с" if mins else f"{secs}с"
    logger.info(f"Scheduled message in {delay}s for {chat_id}")
    return {"content": [{"type": "text", "text": f"Scheduled: отправлю через {time_str}"}]}


@tool("send_video", "Send a video to the user in Telegram (with player/preview)", {"path": str, "caption": str})
async def send_video(args):
    path = args["path"]
    caption = args.get("caption", "")
    chat_id = next(iter(_bot_ref.ALLOWED), None)
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
    chat_id = next(iter(_bot_ref.ALLOWED), None)
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
    chat_id = next(iter(_bot_ref.ALLOWED), None)
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


@tool("react", "Set emoji reactions on user messages. Two modes: 1) Pass 'emoji' alone to react to ALL messages in current batch with same emoji. 2) Pass 'reactions' array to set DIFFERENT emojis on different messages: [{\"msg_id\": 123, \"emoji\": \"😂\"}, {\"msg_id\": 456, \"emoji\": \"🔥\"}]. msg_id values come from [msg_id=X] tags in batched messages. Standard emojis only (😂🔥👍🤔💀🫡🦧🤡👀💪🏆✅❤️🥞🦜 etc).", {
    "emoji": str,
    "reactions": list,
})
async def react(args):
    chat_id = next(iter(_bot_ref.ALLOWED), None)
    if not chat_id:
        return {"content": [{"type": "text", "text": "No ALLOWED_USERS configured"}], "is_error": True}
    try:
        from aiogram.types import ReactionTypeEmoji

        reaction_list = args.get("reactions")
        if reaction_list:
            pairs = reaction_list
        else:
            emoji = args.get("emoji", "👍")
            batch_ids = list(_bot_ref.current_batch_message_ids.get(chat_id, []))
            if not batch_ids:
                return {"content": [{"type": "text", "text": "No messages to react to"}], "is_error": True}
            pairs = [{"msg_id": mid, "emoji": emoji} for mid in batch_ids]

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


kesha_server = create_sdk_mcp_server(
    name="kesha",
    tools=[set_model, set_debounce, toggle_debug, get_bot_status, restart_bot,
           send_photo, send_file, send_video, send_audio, send_voice, schedule_message, react],
)
