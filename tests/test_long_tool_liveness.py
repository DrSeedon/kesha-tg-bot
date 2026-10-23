"""V-42: a long-but-alive tool must not cut the turn; a real hang must stop the CLI.

Prod 14.10.2026: a 7-min bash (SystemMessage task_started, then silence) hit the
300 s tool-phase timeout; the bot said "stream timeout" and only parked the client,
so the CLI finished the turn blind (commit + full answer) and nothing was delivered.
Streams below use the real SDK message types of a persistent ClaudeSDKClient.
"""
import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import response_stream
from tests.test_claude_session_limit import QueueClient, make_session
from tests.test_runtime_limits import FakeBot, FakeRegistry, visible_text
from codex_session import CodexSession


class LoggedClient(QueueClient):
    def __init__(self, log):
        super().__init__()
        self.log = log

    async def interrupt(self):
        self.log.append("interrupt")

    async def disconnect(self):
        self.log.append("disconnect")


def tool_call():
    return AssistantMessage(
        content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "slow.sh"})],
        model="claude-opus-5",
    )


def task_started():
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id="t1", description="slow.sh",
        uuid="u1", session_id="s", tool_use_id="tu1", task_type="local_bash",
    )


def tool_result():
    return UserMessage(content=[ToolResultBlock(tool_use_id="tu1", content="done")])


def answer():
    return [
        AssistantMessage(content=[TextBlock("Готово, скрипт отработал")], model="claude-opus-5"),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=2, session_id="s", result="Готово, скрипт отработал",
            stop_reason="end_turn",
        ),
    ]


def wire(tmp_path, monkeypatch, *, tool, open_work):
    log = []
    session, _ = make_session(tmp_path)
    client = LoggedClient(log)
    session._client = client
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TOOL", tool)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_OPEN_WORK", open_work)
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(session, "claude"))
    return session, client, bot, log


async def run_turn():
    async def noop():
        return None

    typer = asyncio.create_task(noop())
    await typer
    await response_stream._ask_inner(None, "посчитай", 720740564, typer)


@pytest.mark.asyncio
async def test_silent_tool_longer_than_old_threshold_still_delivers(tmp_path, monkeypatch):
    session, client, bot, log = wire(tmp_path, monkeypatch, tool=0.2, open_work=5)
    turn = asyncio.create_task(run_turn())
    await asyncio.sleep(0)
    await client.events.put(tool_call())
    await client.events.put(task_started())
    await asyncio.sleep(0.6)  # 3x the old tool-phase threshold, nothing on the wire
    await client.events.put(TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t1", status="completed",
        output_file="", summary="", uuid="u2", session_id="s", tool_use_id="tu1",
    ))
    await client.events.put(tool_result())
    for event in answer():
        await client.events.put(event)
    await asyncio.wait_for(turn, 5)

    text = visible_text(bot)
    assert "Готово, скрипт отработал" in text
    assert "stream timeout" not in text
    assert log == [], "a live turn must not be interrupted or disconnected"


@pytest.mark.asyncio
async def test_real_stall_stops_the_cli_before_the_notice(tmp_path, monkeypatch):
    session, client, bot, log = wire(tmp_path, monkeypatch, tool=0.1, open_work=0.3)
    notice_at = []
    real_send = bot.send_message

    async def send(chat_id, text, **kw):
        if "stream timeout" in text:
            notice_at.append(list(log))
        return await real_send(chat_id, text, **kw)

    bot.send_message = send
    turn = asyncio.create_task(run_turn())
    await asyncio.sleep(0)
    await client.events.put(tool_call())
    await client.events.put(task_started())
    await asyncio.wait_for(turn, 5)

    assert notice_at and notice_at[0] == ["interrupt", "disconnect"], (
        "CLI must be interrupted and disconnected BEFORE the user is told"
    )
    assert session._client is None and session._pending_disconnect is None
    assert session._is_processing is False
    # the old client is gone: whatever it would still produce reaches no one
    sent_before = len(bot.sent) + len(bot.edits)
    for event in answer():
        await client.events.put(event)
    await asyncio.sleep(0.1)
    assert len(bot.sent) + len(bot.edits) == sent_before
    assert visible_text(bot).count("stream timeout") == 1


@pytest.mark.asyncio
async def test_silent_tool_without_task_messages_is_not_cut(tmp_path, monkeypatch):
    session, client, bot, log = wire(tmp_path, monkeypatch, tool=0.2, open_work=5)
    turn = asyncio.create_task(run_turn())
    await asyncio.sleep(0)
    await client.events.put(tool_call())  # MCP tool / WebFetch: no task_* follows
    await asyncio.sleep(0.6)
    await client.events.put(tool_result())
    for event in answer():
        await client.events.put(event)
    await asyncio.wait_for(turn, 5)

    assert "Готово, скрипт отработал" in visible_text(bot)
    assert "stream timeout" not in visible_text(bot)
    assert log == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("stalled", [False, True])
async def test_hub_image_silence_budget(tmp_path, monkeypatch, runtime, stalled):
    # Run the real runtime -> response_stream path at 1/1000 of the budgets.
    assert response_stream.CHUNK_TIMEOUT_TEXT == 120
    assert response_stream.CHUNK_TIMEOUT_TOOL == 300
    assert response_stream.CHUNK_TIMEOUT_OPEN_WORK == 1800
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TEXT", 0.120)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_TOOL", 0.300)
    monkeypatch.setattr(response_stream, "CHUNK_TIMEOUT_OPEN_WORK", 1.800)
    monkeypatch.setattr(response_stream, "CHAT_EDIT_INTERVAL", 0)
    monkeypatch.setattr("message_log.get_db", lambda: type(
        "Log", (), {"log_assistant": lambda *args: None,
                    "log_response_usage": lambda *args, **kwargs: None},
    )())
    stopped = []
    tool_open = asyncio.Event()
    if runtime == "claude":
        session, client = make_session(tmp_path)
        async def begin():
            await client.events.put(AssistantMessage(content=[ToolUseBlock(
                id="image-call", name="mcp__hub-images__hub_generate_image",
                input={"prompt": "A small green bird", "count": 1},
            )], model="claude-opus-5"))
        async def finish():
            await client.events.put(UserMessage(content=[ToolResultBlock(
                tool_use_id="image-call", content='{"ok":true,"paths":["image.png"]}',
            )]))
            for event in answer():
                await client.events.put(event)
    else:
        session = CodexSession(cwd=str(tmp_path), session_file=tmp_path / "sid")
        session.session_id = "image-thread"
        async def connected():
            pass
        async def request(method, params, **kwargs):
            assert method == "turn/start"
            return {"turn": {"id": "image-turn"}}
        monkeypatch.setattr(session, "_connect", connected)
        monkeypatch.setattr(session, "_request", request)
        item = {"id": "image-call", "type": "mcpToolCall", "server": "hub-images",
                "tool": "hub_generate_image", "arguments": {"count": 1}}
        async def begin():
            session._notifications.put_nowait({
                "method": "item/started", "params": {"item": item, "turnId": "image-turn"},
            })
        async def finish():
            for method, params in [
                ("item/completed", {"item": {**item, "status": "completed"}}),
                ("item/agentMessage/delta", {"delta": "Готово, скрипт отработал"}),
                ("turn/completed", {"turn": {"id": "image-turn", "status": "completed"}}),
            ]:
                session._notifications.put_nowait({"method": method, "params": params})

    async def abort():
        stopped.append(True)
    monkeypatch.setattr(session, "abort_turn", abort, raising=False)
    # Wait for the runtime's liveness event, rather than assuming startup speed.
    send_message = session.send_message
    async def observed(prompt):
        async for chunk in send_message(prompt):
            if chunk["type"] == "liveness" and chunk.get("open"):
                tool_open.set()
            yield chunk
    monkeypatch.setattr(session, "send_message", observed)
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(session, runtime))
    turn = asyncio.create_task(run_turn())
    try:
        await begin()
        await asyncio.wait_for(tool_open.wait(), 1)
        await asyncio.sleep(0.370)
        assert not turn.done(), "370-second MCP silence must leave the turn running"
        if not stalled:
            await finish()
        await asyncio.wait_for(turn, 3)
        if stalled:
            assert visible_text(bot).count("stream timeout") == 1
            assert stopped == [True], "the open-work ceiling must still stop a hung CLI"
        else:
            assert "Готово, скрипт отработал" in visible_text(bot)
            assert "stream timeout" not in visible_text(bot)
            assert stopped == []
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        await session.safe_disconnect()
