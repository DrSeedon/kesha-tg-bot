import asyncio
from types import SimpleNamespace

import pytest

import response_stream


RAW_LIMIT = "You've hit your monthly spend limit · resets 2:20pm (Europe/Berlin)"


class FakeSession:
    async def send_message(self, prompt):
        yield {"type": "text_delta", "content": RAW_LIMIT}
        yield {"type": "error", "kind": "usage_limit", "content": RAW_LIMIT}


class FakeContextSession:
    async def send_message(self, prompt):
        yield {"type": "text_delta", "content": "Prompt is too long"}
        yield {
            "type": "error",
            "kind": "context_limit",
            "content": "Prompt is too long",
        }


class FakeState:
    def __init__(self):
        self.session = FakeSession()
        self.reserve_blocked = False

    def should_stop(self):
        return False

    async def mark_context_reserve_blocked(self):
        self.reserve_blocked = True


class FakeRegistry:
    def __init__(self, session=None):
        self.state = FakeState()
        if session is not None:
            self.state.session = session

    def get(self, chat_id):
        return self.state


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []
        self.next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        message = SimpleNamespace(message_id=self.next_id)
        self.sent.append((chat_id, text, kwargs, message.message_id))
        return message

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeMessage:
    def __init__(self):
        self.answers = []
        self.from_user = SimpleNamespace(language_code="ru")

    async def answer(self, text, **kwargs):
        message = SimpleNamespace(message_id=42)
        self.answers.append((text, kwargs, message.message_id))
        return message


async def completed_typer():
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder", [False, True])
async def test_streamed_raw_limit_is_replaced_by_one_friendly_terminal_outcome(
    monkeypatch, reminder
):
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry())
    message = None if reminder else FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    initial_messages = bot.sent if reminder else message.answers
    assert len(initial_messages) == 1
    assert initial_messages[0][1 if reminder else 0] == RAW_LIMIT
    assert len(bot.edits) == 1
    final_text = bot.edits[0][0]
    assert "лимит" in final_text.lower()
    assert RAW_LIMIT not in final_text
    all_visible = [final_text]
    assert not any("Пустой ответ" in text or "📋" in text for text in all_visible)


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder", [False, True])
async def test_context_limit_is_one_manual_compact_outcome(monkeypatch, reminder):
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(FakeContextSession()))
    message = None if reminder else FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    initial_messages = bot.sent if reminder else message.answers
    assert len(initial_messages) == 1
    assert initial_messages[0][1 if reminder else 0] == "Prompt is too long"
    assert len(bot.edits) == 1
    final_text = bot.edits[0][0]
    assert "/compact" in final_text
    assert "Prompt is too long" not in final_text
    assert "📋" not in final_text
    assert "Пустой ответ" not in final_text


class StaleSession:
    async def send_message(self, prompt):
        yield {
            "type": "error",
            "kind": "session_unavailable",
            "content": "No conversation found",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder", [False, True])
async def test_stale_session_is_one_clear_outcome(monkeypatch, reminder):
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(StaleSession()))
    message = None if reminder else FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    visible = bot.sent if reminder else message.answers
    assert len(visible) == 1
    text = visible[0][1 if reminder else 0]
    assert "/clear" in text
    assert "No conversation found" not in text


class RetryReserveSession:
    def __init__(self):
        self.send_calls = 0
        self.reserve_calls = 0

    async def send_message(self, prompt):
        self.send_calls += 1
        raise RuntimeError("process failed")
        yield

    async def check_context_reserve(self, prompt):
        self.reserve_calls += 1
        return {"ok": False, "reason": "reserve"}

    def reconnect(self):
        return None


@pytest.mark.asyncio
async def test_retry_rechecks_reserve_and_performs_zero_second_query(monkeypatch):
    bot = FakeBot()
    session = RetryReserveSession()
    registry = FakeRegistry(session)
    response_stream.set_bot(bot)
    response_stream.set_registry(registry)
    message = FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    assert session.send_calls == 1
    assert session.reserve_calls == 1
    assert registry.state.reserve_blocked is True
    assert len(message.answers) == 1
    assert "/compact" in message.answers[0][0]
