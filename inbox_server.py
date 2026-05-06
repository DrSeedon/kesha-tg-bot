"""HTTP inbox — receives messages from Orchestra and injects into Kesha."""

import logging
from aiohttp import web

from config import NOTIFY_CHAT

logger = logging.getLogger("kesha.inbox")

INBOX_PORT = 18081
_bot_ref = None
_registry_ref = None


def set_refs(bot, registry):
    global _bot_ref, _registry_ref
    _bot_ref = bot
    _registry_ref = registry


async def handle_inbox(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    message = data.get("message", "")
    sender = data.get("sender", "orchestra")
    chat_id = data.get("chat_id", NOTIFY_CHAT)

    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    if not _bot_ref or not _registry_ref:
        return web.json_response({"error": "bot not ready"}, status=503)

    label = f"📬 [{sender}]"
    tg_text = f"{label}\n{message}"
    try:
        await _bot_ref.send_message(chat_id, tg_text)
    except Exception as e:
        logger.error(f"TG send failed: {e}")

    from chat_state import PendingEntry
    entry = PendingEntry(
        prompt=f"[INBOX from {sender}] {message}",
        message_id=0,
        message=None,
        source="reminder",
    )
    try:
        state = _registry_ref.get(chat_id)
        await state.accept_entry(entry)
    except Exception as e:
        logger.error(f"Inject failed: {e}")
        return web.json_response({"error": str(e)}, status=500)

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
