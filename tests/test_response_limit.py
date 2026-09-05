import asyncio
from types import SimpleNamespace

import pytest

import message_log
import response_stream


RAW_LIMIT = "You've hit your monthly spend limit · resets 2:20pm (Europe/Berlin)"


class FakeSession:
    async def send_message(self, prompt):
        yield {"type": "text_delta", "content": RAW_LIMIT}
        yield {"type": "error", "kind": "usage_limit", "content": RAW_LIMIT}


class FakeContextSession:
    def __init__(self):
        self.calls = 0

    async def send_message(self, prompt):
        self.calls += 1
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


class FloodSession:
    def __init__(self, clock):
        self.clock = clock

    async def send_message(self, prompt):
        self.clock.now = 0.0
        yield {"type": "text_delta", "content": "one"}
        self.clock.now = 2.0
        yield {"type": "text_delta", "content": " two"}
        self.clock.now = 6.0
        yield {"type": "text_delta", "content": " three"}
        yield {"type": "turn_done"}


class FinalUnchangedSession:
    def __init__(self, clock):
        self.clock = clock

    async def send_message(self, prompt):
        self.clock.now = 0.0
        yield {"type": "text_delta", "content": "one"}
        self.clock.now = 4.0
        yield {"type": "text_delta", "content": " two"}
        yield {"type": "turn_done"}


class LongFloodSession:
    def __init__(self, clock):
        self.clock = clock

    async def send_message(self, prompt):
        self.clock.now = 0.0
        yield {"type": "text_delta", "content": "a"}
        self.clock.now = 2.0
        yield {
            "type": "text_delta",
            "content": "b" * (response_stream.TG_MSG_LIMIT - 1),
        }
        self.clock.now = 6.0
        yield {"type": "text_delta", "content": "c"}
        yield {"type": "turn_done"}


class FloodBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.edit_calls = 0

    async def edit_message_text(self, text, **kwargs):
        self.edit_calls += 1
        if self.edit_calls == 1:
            raise RuntimeError("Flood control exceeded. Retry after 30 seconds")
        await super().edit_message_text(text, **kwargs)


class FloodMessage(FakeMessage):
    async def answer(self, text, **kwargs):
        message = SimpleNamespace(message_id=40 + len(self.answers))
        self.answers.append((text, kwargs, message.message_id))
        return message


async def completed_typer():
    return None


@pytest.mark.asyncio
async def test_chat_edit_budget_is_shared_between_bubbles(monkeypatch):
    bot = FakeBot()
    clock = SimpleNamespace(now=10.0)
    sleeps = []

    async def advance(delay):
        sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(response_stream, "monotonic", lambda: clock.now)
    monkeypatch.setattr(response_stream.asyncio, "sleep", advance)
    budget_bot = response_stream._ChatEditBudgetBot(bot)

    await budget_bot.edit_message_text("main", chat_id=7, message_id=1)
    await budget_bot.edit_message_text("tool", chat_id=7, message_id=2)

    assert sleeps == [pytest.approx(response_stream.CHAT_EDIT_INTERVAL)]
    assert [edit[0] for edit in bot.edits] == ["main", "tool"]


@pytest.mark.asyncio
async def test_active_edit_flood_deadline_keeps_stream_visible(monkeypatch):
    bot = FloodBot()
    clock = SimpleNamespace(now=0.0)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(FloodSession(clock)))
    message = FloodMessage()
    typer = asyncio.create_task(completed_typer())
    await typer
    monkeypatch.setattr(response_stream, "monotonic", lambda: clock.now)

    await response_stream._ask_inner(message, "prompt", 7, typer)

    assert [answer[0] for answer in message.answers] == [
        "one",
        "one two",
        "one two three",
    ]
    assert bot.deleted == [(7, 40), (7, 41)]
    assert bot.edit_calls == 1


@pytest.mark.asyncio
async def test_finalization_skips_unchanged_plain_live_text(monkeypatch):
    bot = FakeBot()
    clock = SimpleNamespace(now=0.0)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(FinalUnchangedSession(clock)))
    message = FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer
    monkeypatch.setattr(response_stream, "monotonic", lambda: clock.now)

    await response_stream._ask_inner(message, "prompt", 7, typer)

    assert len(bot.edits) == 1
    assert bot.edits[0][0] == "one two"


@pytest.mark.asyncio
async def test_turn_done_text_is_kept_in_message_history_and_metrics(monkeypatch, caplog):
    bot = FakeBot()
    clock = SimpleNamespace(now=0.0)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(FinalUnchangedSession(clock)))
    message = FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer
    logged = []
    db = SimpleNamespace(log_assistant=lambda chat_id, text: logged.append((chat_id, text)))
    monkeypatch.setattr(message_log, "get_db", lambda: db)
    monkeypatch.setattr(response_stream, "monotonic", lambda: clock.now)

    with caplog.at_level("INFO", logger="kesha"):
        await response_stream._ask_inner(message, "prompt", 7, typer)

    assert logged == [(7, "one two")]
    assert "response 7 chars" in caplog.text


@pytest.mark.asyncio
async def test_flood_fallback_deduplicates_visible_long_text(monkeypatch):
    bot = FloodBot()
    clock = SimpleNamespace(now=0.0)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(LongFloodSession(clock)))
    message = FloodMessage()
    typer = asyncio.create_task(completed_typer())
    await typer
    monkeypatch.setattr(response_stream, "monotonic", lambda: clock.now)

    await response_stream._ask_inner(message, "prompt", 7, typer)

    visible = "a" + "b" * (response_stream.TG_MSG_LIMIT - 1)
    assert [answer[0] for answer in message.answers] == ["a", visible, "c"]
    assert bot.deleted == [(7, 40)]
    assert bot.edit_calls == 1


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
async def test_t3_context_limit_is_one_non_replayed_terminal(monkeypatch, reminder):
    bot = FakeBot()
    response_stream.set_bot(bot)
    session = FakeContextSession()
    response_stream.set_registry(FakeRegistry(session))
    message = None if reminder else FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    initial_messages = bot.sent if reminder else message.answers
    assert len(initial_messages) == 1
    assert initial_messages[0][1 if reminder else 0] == "Prompt is too long"
    assert len(bot.edits) == 1
    final_text = bot.edits[0][0]
    assert "/compact" not in final_text
    assert "повтор" not in final_text.casefold()
    assert "resend" not in final_text.casefold()
    assert "Prompt is too long" not in final_text
    assert "📋" not in final_text
    assert "Пустой ответ" not in final_text
    assert session.calls == 1, "an already-submitted context rejection was replayed"


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
    def __init__(self, reason="reserve"):
        self.send_calls = 0
        self.reserve_calls = 0
        self._reason = reason

    async def send_message(self, prompt):
        self.send_calls += 1
        raise RuntimeError("process failed")
        yield

    async def check_context_reserve(self, prompt):
        self.reserve_calls += 1
        return {"ok": False, "reason": self._reason}

    def reconnect(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["unknown", "runtime_invariant", "runtime_unhealthy"]
)
async def test_unmeasured_context_does_not_cancel_the_retry(reason, monkeypatch):
    """#35: a probe we could not read must not strand a turn mid-retry.

    The `reserve` twin below proves the same harness DOES stop on a real
    measurement, so a passing run here is not the retry loop dying early.
    """
    bot = FakeBot()
    session = RetryReserveSession(reason=reason)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(session))
    message = FakeMessage()
    typer = asyncio.create_task(completed_typer())
    await typer

    await response_stream._ask_inner(message, "prompt", 7, typer)

    assert session.reserve_calls >= 1, "the retry never probed at all"
    assert session.send_calls > 1, (
        f"{reason} cancelled the retry: {session.send_calls} attempt(s)"
    )


@pytest.mark.asyncio
async def test_t3_retry_pressure_refusal_has_zero_second_query_and_no_manual_ux(monkeypatch):
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
    assert registry.state.reserve_blocked is False
    assert len(message.answers) == 1
    assert "/compact" not in message.answers[0][0]
    assert "повтор" not in message.answers[0][0].casefold()
