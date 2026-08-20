"""Фото без подписи держит батч дольше — подпись к нему прилетает голосовым."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import chat_state as chat_state_mod
from chat_state import ChatState, PendingEntry

NORMAL = 3


@pytest.fixture(autouse=True)
def no_message_log(monkeypatch):
    """Батч пишет юзерские строки в общий storage/messages.db — в тесте не пишем:
    иначе мусор утекает в соседние тесты, которые читают последние строки чата."""
    import message_log

    monkeypatch.setattr(message_log, "get_db", lambda: MagicMock())


@pytest.fixture
def armed_delays(monkeypatch):
    """Задержки, с которыми реально взводился дебаунс."""
    recorded: list[float] = []
    original = ChatState._on_debounce_elapsed

    async def spy(self, delay):
        recorded.append(delay)
        return await original(self, delay)

    monkeypatch.setattr(ChatState, "_on_debounce_elapsed", spy)
    return recorded


class Harness:
    def __init__(self):
        self.fired = asyncio.Event()
        self.prompts: list[str] = []

    async def ask_fn(self, message, prompt, chat_id):
        self.prompts.append(prompt)
        self.fired.set()


def make_state(harness, debounce_sec=NORMAL):
    session = MagicMock()
    session.check_context_reserve = AsyncMock(return_value={"ok": True})
    return ChatState(
        chat_id=42,
        session=session,
        bot=MagicMock(),
        debounce_sec=debounce_sec,
        ask_fn=harness.ask_fn,
        set_current_chat_fn=MagicMock(),
        get_lazy_block_fn=MagicMock(return_value=("", [], None)),
        compact_session_fn=AsyncMock(),
        activity_store=MagicMock(),
        work_dir="/tmp",
    )


def entry(prompt, mid, *, bare_photo=False):
    return PendingEntry(
        prompt=prompt,
        message_id=mid,
        message=None,
        source="user",
        reply_target=42,
        bare_photo=bare_photo,
    )


def disarm(state):
    if state._debounce_task:
        state._debounce_task.cancel()


@pytest.mark.asyncio
async def test_bare_photo_waits_longer_than_the_normal_debounce(armed_delays):
    state = make_state(Harness())

    await state.accept_entry(entry("[photo: /a.jpg]", 1, bare_photo=True))
    await asyncio.sleep(0)
    disarm(state)

    assert armed_delays == [chat_state_mod.PHOTO_CAPTION_WAIT_SEC]
    assert chat_state_mod.PHOTO_CAPTION_WAIT_SEC > NORMAL


@pytest.mark.asyncio
async def test_photo_with_caption_keeps_the_normal_debounce(armed_delays):
    state = make_state(Harness())

    await state.accept_entry(entry("[photo: /a.jpg]\nчто тут", 1))
    await asyncio.sleep(0)
    disarm(state)

    assert armed_delays == [NORMAL]


@pytest.mark.asyncio
async def test_text_after_photo_restores_the_normal_debounce(armed_delays):
    state = make_state(Harness())

    await state.accept_entry(entry("[photo: /a.jpg]", 1, bare_photo=True))
    await asyncio.sleep(0)
    await state.accept_entry(entry("а это что", 2))
    await asyncio.sleep(0)
    disarm(state)

    assert armed_delays == [chat_state_mod.PHOTO_CAPTION_WAIT_SEC, NORMAL]


@pytest.mark.asyncio
async def test_voice_after_photo_leaves_in_one_batch():
    """Голосовое внутри окна ожидания едет тем же ходом, что и фото.

    Дебаунс укорочен до 0.05с: если бы фото-окно НЕ сбрасывалось обычным
    сообщением, батч ждал бы PHOTO_CAPTION_WAIT_SEC и тест упал бы по таймауту.
    """
    h = Harness()
    state = make_state(h, debounce_sec=0.05)

    await state.accept_entry(entry("[photo: /a.jpg]", 1, bare_photo=True))
    await state.accept_entry(entry("[voice: /v.oga | что это]", 2))
    await asyncio.wait_for(h.fired.wait(), timeout=3.0)

    assert len(h.prompts) == 1
    assert "[photo: /a.jpg]" in h.prompts[0]
    assert "[voice: /v.oga | что это]" in h.prompts[0]


class FakeChatState:
    """Ловит запись, которую хендлер реально отдал в пайплайн."""

    def __init__(self):
        self.entries = []

    async def media_started(self):
        return (0, 0)

    async def media_finished(self, entry, generation, media_generation):
        self.entries.append(entry)


class FakeRegistry:
    def __init__(self, state):
        self.state = state

    def get(self, chat_id):
        return self.state


class FakeMsg(SimpleNamespace):
    """Всё, чего не спросили явно, — None: у aiogram-сообщения полей десятки."""

    def __getattr__(self, name):
        return None


def photo_message(caption=None):
    photo = SimpleNamespace(file_id="fid", file_unique_id="fuid")
    return FakeMsg(
        chat=SimpleNamespace(id=42),
        from_user=SimpleNamespace(
            id=42,
            username="u",
            full_name="U",
            first_name="U",
            last_name=None,
            language_code="ru",
        ),
        message_id=7,
        photo=[photo],
        caption=caption,
        text=None,
        voice=None,
        video_note=None,
        video=None,
        audio=None,
        document=None,
        sticker=None,
        media_group_id=None,
        date=datetime.now(),
        reply_to_message=None,
        forward_origin=None,
        caption_entities=None,
        entities=None,
    )


@pytest.mark.parametrize("caption,expected", [(None, True), ("что это", False)])
@pytest.mark.asyncio
async def test_photo_handler_marks_bare_photo(monkeypatch, caption, expected):
    import handlers

    state = FakeChatState()
    monkeypatch.setattr(handlers, "_registry", FakeRegistry(state))
    monkeypatch.setattr(handlers, "allowed", lambda uid: True)
    monkeypatch.setattr(handlers, "download_file", AsyncMock(return_value="/tmp/a.jpg"))

    await handlers.h_photo(photo_message(caption))

    assert [e.bare_photo for e in state.entries] == [expected]
