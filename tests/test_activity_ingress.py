import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import handlers
import inbox_server
import reminders
from message_log import ActivityPersistenceError


def _message():
    msg = MagicMock()
    msg.chat.id = 42
    msg.from_user.id = 7
    msg.from_user.language_code = "ru"
    msg.from_user.first_name = "Test"
    msg.from_user.last_name = None
    msg.from_user.username = None
    msg.message_id = 11
    msg.media_group_id = None
    msg.text = None
    msg.caption = None
    msg.forward_date = None
    msg.reply_to_message = None
    return msg


@pytest.mark.asyncio
async def test_text_admission_failure_sends_one_retry_and_admits_nothing(monkeypatch):
    msg = _message()
    state = MagicMock()
    state.accept_entry = AsyncMock(
        side_effect=ActivityPersistenceError("simulated sqlite failure")
    )
    monkeypatch.setattr(handlers, "_registry", MagicMock(get=lambda _chat_id: state))
    send = AsyncMock()
    monkeypatch.setattr(handlers, "_send_safe", send)

    await handlers.enqueue(msg, "hello")

    state.accept_entry.assert_awaited_once()
    send.assert_awaited_once()
    assert "ещё раз" in send.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    ["h_voice", "h_photo", "h_video_note", "h_document", "h_video", "h_audio"],
)
async def test_every_async_media_ingress_persists_before_download(
    monkeypatch, handler_name
):
    msg = _message()
    state = MagicMock()
    state.media_started = AsyncMock(
        side_effect=ActivityPersistenceError("simulated sqlite failure")
    )
    monkeypatch.setattr(handlers, "allowed", lambda _uid: True)
    monkeypatch.setattr(handlers, "_registry", MagicMock(get=lambda _chat_id: state))
    download = AsyncMock()
    monkeypatch.setattr(handlers, "download_file", download)
    send = AsyncMock()
    monkeypatch.setattr(handlers, "_send_safe", send)

    await getattr(handlers, handler_name)(msg)

    state.media_started.assert_awaited_once()
    download.assert_not_awaited()
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_album_persists_before_first_download(monkeypatch):
    msg = _message()
    state = MagicMock()
    state.media_started = AsyncMock(
        side_effect=ActivityPersistenceError("simulated sqlite failure")
    )
    monkeypatch.setattr(handlers, "allowed", lambda _uid: True)
    monkeypatch.setattr(handlers, "_registry", MagicMock(get=lambda _chat_id: state))
    download = AsyncMock()
    monkeypatch.setattr(handlers, "download_file", download)
    send = AsyncMock()
    monkeypatch.setattr(handlers, "_send_safe", send)

    album_handler = getattr(handlers.h_media_album, "__wrapped__", handlers.h_media_album)
    await album_handler([msg])

    state.media_started.assert_awaited_once()
    download.assert_not_awaited()
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_compact_admission_failure_is_friendly(monkeypatch):
    msg = _message()
    state = MagicMock(is_busy=False)
    state.request_compact = AsyncMock(
        side_effect=ActivityPersistenceError("simulated sqlite failure")
    )
    monkeypatch.setattr(handlers, "allowed", lambda _uid: True)
    monkeypatch.setattr(handlers, "_registry", MagicMock(get=lambda _chat_id: state))
    send = AsyncMock()
    monkeypatch.setattr(handlers, "_send_safe", send)

    await handlers.h_compact(msg)

    state.request_compact.assert_awaited_once()
    send.assert_awaited_once()
    assert "ещё раз" in send.await_args.args[1]


@pytest.mark.asyncio
async def test_inbox_failure_returns_generic_503_before_telegram_echo(monkeypatch):
    request = MagicMock()
    request.json = AsyncMock(
        return_value={"message": "hello", "sender": "test", "chat_id": 42}
    )
    state = MagicMock()
    state.accept_entry = AsyncMock(
        side_effect=ActivityPersistenceError("database secret detail")
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    monkeypatch.setattr(inbox_server, "ALLOWED", [])
    monkeypatch.setattr(inbox_server, "_bot_ref", bot)
    monkeypatch.setattr(
        inbox_server,
        "_registry_ref",
        MagicMock(get=lambda _chat_id: state),
    )

    response = await inbox_server.handle_inbox(request)

    assert response.status == 503
    assert json.loads(response.body)["error"] == "message was not accepted; retry"
    assert b"database secret detail" not in response.body
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_urgent_admission_failure_uses_existing_plain_fallback(monkeypatch):
    async def rejected(_chat_id, _payload):
        raise ActivityPersistenceError("simulated sqlite failure")

    bot = MagicMock()
    bot.send_message = AsyncMock()
    monkeypatch.setattr(reminders, "_urgent_llm_handler", rejected)

    await reminders._run_urgent_llm("take medicine", 42, MagicMock(), bot)

    bot.send_message.assert_awaited_once_with(42, "⏰ take medicine")
