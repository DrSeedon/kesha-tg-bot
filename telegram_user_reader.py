"""On-demand, read-only access to the owner's Telegram cloud dialogs.

The user session and API credentials live under storage/ with mode 0600. This
module never sends messages, marks chats read, or downloads media.
"""

import asyncio
import os
from pathlib import Path

from telethon import TelegramClient


ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "storage" / "telegram-user"
SESSION = PRIVATE / "owner"
CONFIG = PRIVATE / "credentials"
_lock = asyncio.Lock()


def _credentials():
    if not CONFIG.exists():
        raise RuntimeError("Telegram user credentials are not configured")
    if CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("Telegram user credentials have unsafe permissions")
    values = {}
    for line in CONFIG.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"API_ID", "API_HASH"}:
            values[key] = value.strip()
    if not values.get("API_ID") or not values.get("API_HASH"):
        raise RuntimeError("Telegram user credentials are incomplete")
    return int(values["API_ID"]), values["API_HASH"]


async def _run(operation):
    async with _lock:
        api_id, api_hash = _credentials()
        client = TelegramClient(str(SESSION), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram user session is not authorized")
            result = await operation(client)
            if SESSION.with_suffix(".session").exists():
                os.chmod(SESSION.with_suffix(".session"), 0o600)
            return result
        finally:
            await client.disconnect()


def _summary(message):
    return {
        "id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "sender_id": message.sender_id,
        "text": (message.raw_text or "")[:4000],
        "has_media": bool(message.media),
    }


async def list_dialogs(limit=50):
    limit = max(1, min(int(limit), 100))

    async def op(client):
        return [
            {"id": dialog.id, "name": dialog.name, "type": type(dialog.entity).__name__}
            async for dialog in client.iter_dialogs(limit=limit)
        ]

    return await _run(op)


async def read_dialog(dialog_id, limit=20):
    limit = max(1, min(int(limit), 50))

    async def op(client):
        entity = await client.get_entity(int(dialog_id))
        return [_summary(message) async for message in client.iter_messages(entity, limit=limit)]

    return await _run(op)


async def search_messages(query, dialog_id=None, limit=20):
    if not query or len(query.strip()) < 2:
        raise ValueError("Search query must contain at least two characters")
    limit = max(1, min(int(limit), 50))

    async def op(client):
        entity = await client.get_entity(int(dialog_id)) if dialog_id is not None else None
        return [_summary(message) async for message in client.iter_messages(entity, search=query, limit=limit)]

    return await _run(op)
