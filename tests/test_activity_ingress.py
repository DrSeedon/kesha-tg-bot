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


@pytest.mark.asyncio
async def test_ingress_during_processing_defers_without_blocking_the_stream():
    """A message arriving mid-turn must not stall the streaming edit loop.

    Regression guard for the reported "bot freezes while streaming when the user
    types": ingress may only defer. It must not await the Claude query lock
    (no inject) and must not hold ChatState._lock across any network await,
    otherwise the response_stream edit loop starves behind it.
    """
    import asyncio

    import chat_state as chat_state_mod
    from chat_state import ChatPhase, ChatState, PendingEntry

    session = MagicMock()
    session.inject = AsyncMock(
        side_effect=AssertionError("ingress must not inject during a turn")
    )
    state = ChatState(
        chat_id=42,
        session=session,
        bot=MagicMock(),
        debounce_sec=3,
        ask_fn=AsyncMock(),
        set_current_chat_fn=MagicMock(),
        get_lazy_block_fn=MagicMock(return_value=("", [], None)),
        compact_session_fn=AsyncMock(),
        activity_store=MagicMock(),
        work_dir="/tmp",
    )
    state.phase = ChatPhase.PROCESSING

    # Streaming loop stand-in: ticks while the turn is active.
    ticks = 0
    stop = False

    async def edit_loop():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    loop_task = asyncio.create_task(edit_loop())
    await asyncio.sleep(0)
    before = ticks

    for i in range(5):
        await state.accept_entry(
            PendingEntry(prompt=f"typed {i}", message_id=100 + i, source="user")
        )

    await asyncio.sleep(0)
    stop = True
    await loop_task

    session.inject.assert_not_awaited()
    assert len(state.deferred) == 5, "every mid-turn arrival must be deferred"
    assert not state.pending, "mid-turn arrivals must not enter the active batch"
    assert ticks > before, "edit loop must keep running while ingress is admitted"


@pytest.mark.asyncio
async def test_runtime_invariant_message_renders_without_keyerror():
    """Codex [blocking]: the localized string contains {expected}.

    `config.t()` already calls .format(**kw), so passing the value through a
    second .format() raised KeyError on the normal Telegram path — the users
    saw a crash instead of the real reason.
    """
    from types import SimpleNamespace

    import chat_state as cs
    from chat_state import ChatState, PendingEntry

    sent = []

    class Msg:
        from_user = SimpleNamespace(language_code="ru")

        async def answer(self, text, **kwargs):
            sent.append(text)
            return SimpleNamespace(message_id=1)

    state = ChatState(
        chat_id=42,
        session=MagicMock(),
        bot=MagicMock(),
        debounce_sec=3,
        ask_fn=AsyncMock(),
        set_current_chat_fn=MagicMock(),
        get_lazy_block_fn=MagicMock(return_value=("", [], None)),
        compact_session_fn=AsyncMock(),
        activity_store=MagicMock(),
        work_dir="/tmp",
    )
    entry = PendingEntry(prompt="x", message_id=1, message=Msg())

    await state._send_batch_terminal(
        [entry], "context_runtime_invariant", expected="claude-opus-5[1m]"
    )

    assert sent and "claude-opus-5[1m]" in sent[0], sent
    assert "{expected}" not in sent[0]


@pytest.mark.asyncio
async def test_t3_plain_terminal_keys_still_render():
    """Keys without placeholders must survive the shared format path."""
    from types import SimpleNamespace

    from chat_state import ChatState, PendingEntry

    sent = []

    class Msg:
        from_user = SimpleNamespace(language_code="ru")

        async def answer(self, text, **kwargs):
            sent.append(text)
            return SimpleNamespace(message_id=1)

    state = ChatState(
        chat_id=42,
        session=MagicMock(),
        bot=MagicMock(),
        debounce_sec=3,
        ask_fn=AsyncMock(),
        set_current_chat_fn=MagicMock(),
        get_lazy_block_fn=MagicMock(return_value=("", [], None)),
        compact_session_fn=AsyncMock(),
        activity_store=MagicMock(),
        work_dir="/tmp",
    )
    entry = PendingEntry(prompt="x", message_id=1, message=Msg())

    for key in (
        "context_auto_compact_failed",
        "context_unknown",
        "context_usage_limit",
        "context_runtime_unhealthy",
    ):
        await state._send_batch_terminal([entry], key)

    assert len(sent) == 4


def test_t3_every_context_preflight_reason_has_strings_in_both_languages():
    """#14 shipped a KeyError by adding a reason and updating only one path.

    Every reason `check_context_reserve` can return must map to a key that
    exists in ru AND en, or the refusal itself crashes at send time.
    """
    from config import STRINGS

    keys = {
        "context_auto_compact_failed",
        "context_unknown",
        "context_usage_limit",
        "context_runtime_invariant",
        "context_runtime_unhealthy",
        "session_unavailable",
        "compact_floor",
    }
    for lang in ("ru", "en"):
        missing = keys - set(STRINGS[lang])
        assert not missing, f"{lang} missing {missing}"
