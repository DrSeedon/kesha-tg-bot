import asyncio
from types import SimpleNamespace

import pytest

import response_stream


RAW_LIMIT = "You've hit your monthly spend limit · resets 2:20pm (Europe/Berlin)"


class FakeSession:
    async def send_message(self, prompt):
        yield {"type": "text_delta", "content": RAW_LIMIT}
        yield {"type": "error", "kind": "usage_limit", "content": RAW_LIMIT}


class FakeState:
    def __init__(self):
        self.session = FakeSession()

    def should_stop(self):
        return False


class FakeRegistry:
    def __init__(self):
        self.state = FakeState()

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

