"""V-43: background work of the persistent CLI behaves as in Claude Code.

Prod 14.10.2026, chat 720740564: Kesha launched 9 background agents and ended
its turn. The CLI then ran turns of its own on every task notification ("7 of
9 done") that nobody read; the undrained SDK buffer starved the control
channel, the admission probe timed out twice, the bot reconnected and killed
two agents, and the new CLI's first Result (0 turns, a notification) was taken
for the answer to the user's question ("🤷 Пустой ответ").

Streams below use real SDK message types; the fake client echoes each prompt's
uuid when it takes it into a turn, as the CLI does under --replay-user-messages
(research.md §2). The buffer test drives the real SDK client and Query.
"""
import asyncio
import json

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ToolUseBlock,
)

import claude_session
import response_stream
from chat_state import ChatPhase, ChatState, PendingEntry
from tests.sdk_echo import echo, prompt_frames, text_of
from tests.test_claude_session_limit import QueueClient, context_usage, make_session
from tests.test_runtime_limits import FakeBot, visible_text

CID = 720740564
NOTIFICATION = {"kind": "task-notification"}


class CliClient(QueueClient):
    """Logs every way the bot could kill the CLI and its background agents."""

    def __init__(self):
        super().__init__()
        self.log = []
        self.context_usage = context_usage(300_000)
        self.echo_on_query = True  # False: the test decides when the CLI takes it
        self.untaken = []

    async def query(self, prompt):
        if self.echo_on_query:
            return await super().query(prompt)
        frames = await prompt_frames(prompt)
        self.queries.append(text_of(frames))
        self.untaken.extend(frames)

    def take(self):
        """The CLI takes the queued prompt(s) into its current turn."""
        for frame in echo(self.untaken):
            self.events.put_nowait(frame)
        self.untaken = []

    async def interrupt(self):
        self.log.append("interrupt")

    async def disconnect(self):
        self.log.append("disconnect")


def say(text):
    return AssistantMessage(content=[TextBlock(text)], model="claude-opus-5-5")


def delta(text):
    return StreamEvent(
        uuid="d", session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def done(origin=None, turns=1):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=turns, session_id="s", result=None, stop_reason="end_turn",
        origin=origin,
    )


def agent_started(n):
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=f"agent-{n}", description="part",
        uuid=f"ts{n}", session_id="s", tool_use_id=f"tu{n}", task_type="local_agent",
    )


def agent_finished(n, status="completed"):
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=f"agent-{n}", status=status,
        output_file="", summary="", uuid=f"tn{n}", session_id="s", tool_use_id=f"tu{n}",
    )


def notification_turn(n, text):
    """What the CLI emits on its own when agent n finishes (spike: init, stream, Result)."""
    return [agent_finished(n), delta(text), done(origin=NOTIFICATION)]


class Store:
    def begin_activity(self, *_a, **_k):
        return ""

    def finish_activity(self, *_a, **_k):
        return ""


class Registry:
    def __init__(self, state):
        self.state = state

    def get(self, _chat_id):
        return self.state


@pytest.fixture
def chat(tmp_path, monkeypatch):
    """A real ChatState + ClaudeSession + response_stream, faked only at the CLI and Telegram."""
    import message_log
    from unittest.mock import MagicMock

    monkeypatch.setattr(message_log, "get_db", lambda: MagicMock())
    session, _ = make_session(tmp_path)
    client = CliClient()
    session._client = client
    bot = FakeBot()

    async def no_compact(*_a, **_k):
        raise AssertionError("no compact expected")

    state = ChatState(
        chat_id=CID, session=session, bot=bot, debounce_sec=0,
        ask_fn=response_stream._ask, set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: ("", [], []), compact_session_fn=no_compact,
        activity_store=Store(), work_dir="/tmp",
    )
    response_stream.set_bot(bot)
    response_stream.set_registry(Registry(state))

    async def no_typing(_cid):
        return None

    monkeypatch.setattr(response_stream, "typing_loop", no_typing)
    return state, session, client, bot


async def settle(state, ceiling=5.0):
    """Wait until the chat is idle again — bounded, so a hang fails, not hangs."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ceiling
    while True:
        await asyncio.sleep(0.01)
        task = state._processing_task
        if state.phase == ChatPhase.IDLE and (task is None or task.done()):
            return
        if loop.time() > deadline:
            raise AssertionError(f"chat stuck in {state.phase}")


async def ask(state, text, message_id):
    await state.accept_entry(PendingEntry(
        prompt=text, message_id=message_id, message=None, source="user", reply_target=CID,
    ))


async def launch_nine_agents(state, client):
    """Turn 1: Kesha starts 9 background agents and ends its turn."""
    await ask(state, "переведи мод", 1)
    await asyncio.sleep(0.05)
    for n in range(9):
        client.events.put_nowait(agent_started(n))
    client.events.put_nowait(say("🚀 Запустил 9 агентов"))
    client.events.put_nowait(done(turns=26))
    await settle(state)


@pytest.mark.asyncio
async def test_notification_turns_reach_the_chat_without_killing_the_cli(chat):
    state, session, client, bot = chat
    await launch_nine_agents(state, client)

    for n, text in ((0, "Готово 1 из 9"), (1, "Готово 2 из 9")):
        for frame in notification_turn(n, text):
            client.events.put_nowait(frame)
        await asyncio.sleep(0.05)
        await settle(state)

    shown = visible_text(bot)
    assert "Запустил 9 агентов" in shown
    assert "Готово 1 из 9" in shown and "Готово 2 из 9" in shown
    assert "Пустой ответ" not in shown
    assert client.log == [], "background work must not interrupt or disconnect the CLI"
    assert session.background_agents_open() == 7


@pytest.mark.asyncio
async def test_question_during_a_cli_turn_is_steered_and_answered(chat):
    """V-44: the message goes to the CLI at once; the CLI runs it after its own turn."""
    state, session, client, bot = chat
    await launch_nine_agents(state, client)
    client.echo_on_query = False

    # A notification turn starts and streams; the user writes in the middle of it.
    client.events.put_nowait(agent_finished(0))
    client.events.put_nowait(delta("Готово 7 из 9"))
    await asyncio.sleep(0.05)
    assert state.phase == ChatPhase.PROCESSING
    await ask(state, "А что там с отзывами?", 2)
    await asyncio.sleep(0.05)
    assert client.queries[-1].endswith("А что там с отзывами?"), "sent before the CLI turn ended"

    client.events.put_nowait(done(origin=NOTIFICATION))
    client.take()  # the CLI dequeues it as its next turn
    client.events.put_nowait(say("Отзывы хорошие"))
    client.events.put_nowait(done())
    await settle(state)

    shown = visible_text(bot)
    assert "Готово 7 из 9" in shown and "Отзывы хорошие" in shown
    assert "Пустой ответ" not in shown
    assert not state.deferred
    assert client.log == []


@pytest.mark.asyncio
async def test_admission_probe_answers_while_the_cli_works_and_never_reconnects(chat, monkeypatch):
    state, session, client, _bot = chat
    monkeypatch.setattr(claude_session, "CONTEXT_PROBE_TIMEOUT_S", 0.05)
    await launch_nine_agents(state, client)

    class SilentProbe(CliClient):
        async def get_context_usage(self):
            await asyncio.sleep(3600)

    # Even a probe that never answers must not cost the agents their CLI while
    # the stream itself is alive: the context is merely unmeasured.
    client.__class__ = SilentProbe
    outcome = await session.check_context_reserve("hello")

    assert outcome["reason"] == "unmeasured"
    assert session._client is client and session._connected
    assert client.log == []


@pytest.mark.asyncio
async def test_foreign_empty_result_before_our_turn_is_not_the_answer(chat):
    """The incident's tail: the new CLI first replays a notification (0 turns)."""
    state, session, client, bot = chat
    client.echo_on_query = False
    await ask(state, "А погуглю вообще, что это за мод", 1)
    await asyncio.sleep(0.05)
    client.events.put_nowait(agent_finished(0, status="stopped"))
    client.events.put_nowait(delta(""))
    client.events.put_nowait(done(origin=NOTIFICATION, turns=0))
    await asyncio.sleep(0.05)
    assert state.phase == ChatPhase.PROCESSING, "a foreign Result must not end our answer"
    client.take()
    client.events.put_nowait(say("Это мод RLS Career Overhaul"))
    client.events.put_nowait(done())
    await settle(state)

    shown = visible_text(bot)
    assert "RLS Career Overhaul" in shown
    assert "Пустой ответ" not in shown


@pytest.mark.asyncio
async def test_prompt_merged_into_a_notification_turn_counts_as_ours(tmp_path):
    """Prod 10:30:22: notification and our message were dequeued into ONE turn."""
    session, _ = make_session(tmp_path)
    client = CliClient()
    client.echo_on_query = False
    session._client = client

    stream = asyncio.create_task(_collect(session.send_message("вопрос")))
    await asyncio.sleep(0.05)
    client.events.put_nowait(agent_finished(0))
    client.events.put_nowait(delta("Агент закончил. "))
    client.take()  # taken mid-turn
    client.events.put_nowait(delta("А на вопрос отвечаю так"))
    client.events.put_nowait(done(origin=NOTIFICATION))
    chunks = await asyncio.wait_for(stream, 2)

    assert [c["content"] for c in chunks if c["type"] == "text_delta"] == [
        "Агент закончил. ", "А на вопрос отвечаю так",
    ]
    assert not any(c["type"] == "turn_done" for c in chunks), "the turn closed OUR question"


@pytest.mark.asyncio
async def test_prompt_never_taken_is_a_clear_error_not_an_endless_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_session, "PROMPT_PICKUP_TIMEOUT_S", 0.2)
    session, _ = make_session(tmp_path)

    client = CliClient()
    client.echo_on_query = False  # the CLI swallows it: no echo, no turn
    session._client = client
    # Hard ceiling far below any suite timeout: waiting forever FAILS here.
    chunks = await asyncio.wait_for(_collect(session.send_message("алло")), 2)

    assert chunks == [{
        "type": "error", "kind": "prompt_lost",
        "content": "Claude CLI did not take the message into a turn",
    }]


@pytest.mark.asyncio
async def test_prompt_lost_reaches_the_user_once(chat, monkeypatch):
    state, session, client, bot = chat
    monkeypatch.setattr(claude_session, "PROMPT_PICKUP_TIMEOUT_S", 0.2)

    client.echo_on_query = False
    await ask(state, "алло", 1)
    await settle(state)

    shown = visible_text(bot)
    assert "не взял сообщение" in shown
    assert "Пустой ответ" not in shown


@pytest.mark.asyncio
async def test_real_stall_inside_a_cli_turn_still_stops_the_cli(chat, monkeypatch):
    state, session, client, bot = chat
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TEXT", 0.2)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TOOL", 0.2)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_OPEN_WORK", 0.2)
    await launch_nine_agents(state, client)

    client.events.put_nowait(delta("Сливаю части"))  # turn opens... then silence
    await settle(state, ceiling=3)

    assert client.log[:2] == ["interrupt", "disconnect"]
    assert "stream timeout" in visible_text(bot)


@pytest.mark.asyncio
async def test_quiet_gap_between_turns_is_not_a_stall(chat, monkeypatch):
    state, session, client, bot = chat
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TEXT", 0.1)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TOOL", 0.1)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_OPEN_WORK", 0.1)
    await launch_nine_agents(state, client)

    await asyncio.sleep(0.5)  # agents work, the CLI emits nothing at all
    for frame in notification_turn(0, "Готово 1 из 9"):
        client.events.put_nowait(frame)
    await asyncio.sleep(0.05)
    await settle(state)

    assert client.log == []
    assert "Готово 1 из 9" in visible_text(bot)


@pytest.mark.asyncio
async def test_compact_waits_for_background_agents(chat):
    state, session, client, bot = chat
    await launch_nine_agents(state, client)

    assert await state.request_compact() is True
    assert state.phase == ChatPhase.IDLE and state.compact_after_background
    assert any("Фоновые агенты ещё работают (9)" in text for _, text in bot.sent)
    assert client.log == []


async def _collect(stream):
    return [chunk async for chunk in stream]


# --- The buffer itself, on the real SDK client ---------------------------


class CliTransport:
    """CLI side of the wire: answers every control request after the backlog."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        return None

    async def write(self, data):
        msg = json.loads(data)
        if msg.get("type") == "control_request":
            response = context_usage(1000) if msg["request"].get("subtype") == "get_context_usage" else {}
            self.q.put_nowait({"type": "control_response", "response": {
                "subtype": "success", "request_id": msg["request_id"], "response": response}})

    async def read_messages(self):
        while True:
            yield await self.q.get()

    async def close(self):
        return None

    def is_ready(self):
        return True

    async def end_input(self):
        return None


@pytest.mark.asyncio
async def test_frames_between_turns_never_starve_the_control_channel(tmp_path, monkeypatch):
    """500 frames arrive while nobody asks anything; the probe must still answer.

    Without our reader the SDK's 100-slot buffer fills and its read loop never
    reaches the control_response (research.md §1: 100 answer, 101 time out).
    """
    monkeypatch.setattr(claude_session, "CONTEXT_PROBE_TIMEOUT_S", 0.5)
    transport = CliTransport()
    client = ClaudeSDKClient(transport=transport)
    await client.connect()
    import config

    session = claude_session.ClaudeSession(cwd=".", model=config.MODEL, session_file=tmp_path / "sid")
    session._client = client
    session._connected = True
    session._attach_stream()

    for n in range(500):
        transport.q.put_nowait({
            "type": "system", "subtype": "task_progress", "task_id": "agent-0",
            "description": "", "uuid": f"p{n}", "session_id": "s",
            "usage": {"total_tokens": 1, "tool_uses": 0, "duration_ms": 1},
        })
    await asyncio.sleep(0.1)

    outcome = await session.check_context_reserve("hello")
    assert outcome["ok"] is True, outcome
    session._stream.stop()
    await client.disconnect()
