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
    assert session._is_processing is False and session._expected_results == 0
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
