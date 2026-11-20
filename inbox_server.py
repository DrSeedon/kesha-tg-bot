"""HTTP inbox — receives authenticated messages from Orchestra and injects into Kesha."""

import hmac
import logging
import os
from aiohttp import web

from config import ALLOWED, NOTIFY_CHAT
from message_log import ActivityPersistenceError

logger = logging.getLogger("kesha.inbox")

INBOX_PORT = 18081
_bot_ref = None
_registry_ref = None


def set_refs(bot, registry):
    global _bot_ref, _registry_ref
    _bot_ref = bot
    _registry_ref = registry


def _expected_inbox_token() -> str:
    explicit = os.getenv("KESHA_INBOX_TOKEN", "").strip()
    if explicit:
        return explicit
    try:
        import limits
        return limits.orchestra_token()
    except Exception:
        return ""


def _check_inbox_auth(request: web.Request) -> web.Response | None:
    expected = _expected_inbox_token()
    if not expected:
        return web.json_response({"error": "inbox auth token not configured"}, status=401)
    headers = getattr(request, "headers", None) or {}
    auth = ""
    if hasattr(headers, "get"):
        auth = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return web.json_response({"error": "missing bearer token"}, status=401)
    provided = auth[len("Bearer "):].strip()
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        return web.json_response({"error": "invalid bearer token"}, status=403)
    return None


async def handle_inbox(request: web.Request) -> web.Response:
    auth_err = _check_inbox_auth(request)
    if auth_err is not None:
        return auth_err

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    message = data.get("message", "")
    sender = data.get("sender", "orchestra")
    chat_id = data.get("chat_id", NOTIFY_CHAT)

    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid chat_id"}, status=400)

    if not ALLOWED or chat_id not in ALLOWED:
        return web.json_response({"error": "chat_id not allowed"}, status=403)

    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    if not _bot_ref or not _registry_ref:
        return web.json_response({"error": "bot not ready"}, status=503)

    from chat_state import PendingEntry
    entry = PendingEntry(
        prompt=f"[INBOX from {sender}] {message}",
        message_id=0,
        message=None,
        source="reminder",
    )
    try:
        state = _registry_ref.get(chat_id)
        native = state.runtime_id == "antigravity"
        if not native:
            await state.accept_entry(entry)
    except ActivityPersistenceError:
        logger.error(f"Inbox activity admission failed for chat {chat_id}")
        return web.json_response({"error": "message was not accepted; retry"}, status=503)
    except Exception as e:
        logger.error(f"Inject failed: {e}")
        return web.json_response({"error": "message was not accepted"}, status=500)

    label = f"📬 [{sender}]"
    tg_text = f"{label}\n{message}"
    try:
        if native:
            from telegram_io import split_msg
            for part in split_msg(tg_text):
                await _bot_ref.send_message(chat_id, part, parse_mode=None)
        else:
            await _bot_ref.send_message(chat_id, tg_text)
    except Exception as e:
        logger.error(f"TG send failed: {e}")
        if native:
            return web.json_response({"error": "Telegram delivery failed; retry"}, status=503)

    logger.info(f"Inbox: {sender} → chat {chat_id} ({len(message)} chars)")
    return web.json_response({"ok": True})


async def start_inbox_server():
    app = web.Application()
    app.router.add_post("/inbox", handle_inbox)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", INBOX_PORT)
    await site.start()
    logger.info(f"Inbox server on http://127.0.0.1:{INBOX_PORT}/inbox")
    return runner
