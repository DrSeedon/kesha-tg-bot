"""#28 — раскладка стрима: текст одним куском, тулы одним пузырём, без болтовни субагентов."""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

import response_stream
from claude_session import (
    ClaudeSession,
    EXPECTED_CONTEXT_MODEL,
    EXPECTED_MAX_OUTPUT_TOKENS,
)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=900 + len(self.sent))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeMessage:
    def __init__(self):
        self.answers = []
        self.from_user = SimpleNamespace(language_code="ru")

    async def answer(self, text, **kwargs):
        mid = 100 + len(self.answers)
        self.answers.append((text, kwargs, mid))
        return SimpleNamespace(message_id=mid)


class InterleavedSession:
    """Типовой ход с субагентом: текст → тул → текст → тул → текст."""

    async def send_message(self, prompt):
        yield {"type": "text_delta", "content": "часть один "}
        yield {"type": "tool", "name": "WebSearch", "input": {"query": "a"}}
        yield {"type": "tool_done"}
        yield {"type": "text_delta", "content": "часть два "}
        yield {"type": "tool", "name": "WebFetch", "input": {"url": "b"}}
        yield {"type": "tool_done"}
        yield {"type": "text_delta", "content": "часть три"}
        yield {"type": "turn_done"}


class FakeState:
    def __init__(self, session):
        self.session = session

    def should_stop(self):
        return False


class FakeRegistry:
    def __init__(self, session):
        self.state = FakeState(session)

    def get(self, chat_id):
        return self.state


async def completed_typer():
    return None


def _bubbles(message):
    return [a for a in message.answers if a[1].get("parse_mode") == "Markdown"]


def _text_msgs(message):
    return [a for a in message.answers if a[1].get("parse_mode") != "Markdown"]


@pytest.mark.asyncio
async def test_interleaved_tools_do_not_split_the_answer(monkeypatch):
    bot = FakeBot()
    clock = SimpleNamespace(now=0.0)

    def tick():
        # Часы бегут вперёд на каждый запрос — бюджет правок никогда не спит,
        # тест не зависит от реального времени.
        clock.now += 10.0
        return clock.now

    monkeypatch.setattr(response_stream, "monotonic", tick)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(InterleavedSession()))
    message = FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    # Ровно один пузырь тулов, и он ВЫШЕ ответа: сначала работа, потом текст.
    assert len(_bubbles(message)) == 1
    alive = [a for a in message.answers if a[2] not in {d[1] for d in bot.deleted}]
    text_alive = [a for a in alive if a[1].get("parse_mode") != "Markdown"]
    bubble = _bubbles(message)[0]
    assert len(text_alive) == 1
    assert bubble[2] < text_alive[0][2], "пузырь тулов обязан быть выше ответа"
    # Текст не потерян и не разорван.
    final = [e[0] for e in bot.edits if "часть" in e[0]]
    assert final and final[-1] == "часть один часть два часть три"
    # Оба тула в одном пузыре.
    bubble_texts = [e[0] for e in bot.edits if "Сделано" in e[0] or "Работаю" in e[0]]
    assert bubble_texts and "WebSearch" in bubble_texts[-1] and "WebFetch" in bubble_texts[-1]


class QueueClient:
    def __init__(self):
        self.events = asyncio.Queue()

    async def query(self, text):
        return None

    async def receive_messages(self):
        while True:
            yield await self.events.get()

    async def get_context_usage(self):
        return None


def make_session(tmp_path):
    import config

    session = ClaudeSession(
        cwd=".", model=config.MODEL, session_file=tmp_path / "session"
    )
    client = QueueClient()
    session._client = client
    session._connected = True

    async def connected(**kwargs):
        return None

    session._ensure_connected = connected
    return session, client


def _result():
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sid",
        result=None,
        model_usage={
            EXPECTED_CONTEXT_MODEL: {"maxOutputTokens": EXPECTED_MAX_OUTPUT_TOKENS}
        },
    )


def _assistant(text=None, tool=None, parent=None):
    blocks = []
    if text is not None:
        blocks.append(TextBlock(text=text))
    if tool is not None:
        blocks.append(ToolUseBlock(id="tu-1", name=tool, input={}))
    return AssistantMessage(content=blocks, model="m", parent_tool_use_id=parent)


@pytest.mark.asyncio
async def test_subagent_narration_never_reaches_the_user(tmp_path):
    session, client = make_session(tmp_path)
    for msg in (
        _assistant(text="Кеша говорит"),
        _assistant(text="I'll research all four directions", parent="tu-agent"),
        _assistant(tool="WebSearch", parent="tu-agent"),
        StreamEvent(
            uuid="u1",
            session_id="sid",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "subagent typing"},
            },
            parent_tool_use_id="tu-agent",
        ),
        StreamEvent(
            uuid="u2",
            session_id="sid",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Кеша печатает"},
            },
            parent_tool_use_id=None,
        ),
        _result(),
    ):
        client.events.put_nowait(msg)

    chunks = [c async for c in session.send_message("hi")]

    texts = [c["content"] for c in chunks if c["type"] in ("text", "text_delta")]
    assert texts == ["Кеша говорит", "Кеша печатает"]
    # Тулы субагента остаются — это прогресс в пузыре.
    assert [c["name"] for c in chunks if c["type"] == "tool"] == ["WebSearch"]
