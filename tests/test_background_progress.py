"""V-47: background agents' work does not spam the chat or tear the answer.

Prod 18.10.2026 00:20-00:31 KRSK, chat 720740564: Kesha handed an analysis to
background agents. Every finished agent made the CLI run a turn of its own, and
each turn's "⏳ still counting: X done" became a NEW message: 16 pushes in half
an hour, at night. Inside one turn the main text was cut into 15 pieces
mid-word, with bubbles of the background agents' Bash calls between them.

Real SDK message types, real ChatState/ClaudeSession/response_stream; only the
CLI and Telegram are faked (fixture from test_background_turns).
"""
import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock

from tests.test_background_turns import (  # noqa: F401  (chat is a fixture)
    agent_started,
    ask,
    chat,
    delta,
    done,
    launch_nine_agents,
    notification_turn,
    settle,
)
from tests.test_runtime_limits import visible_text


def background_agent_tool(command, agent=0):
    """A Bash call of background agent `agent`; its Agent call returned long ago."""
    return AssistantMessage(
        content=[ToolUseBlock(id=f"sub-{command}", name="Bash", input={"command": command})],
        model="claude-opus-5-5",
        parent_tool_use_id=f"tu{agent}",
    )


async def feed(state, client, frames):
    for frame in frames:
        client.events.put_nowait(frame)
    await asyncio.sleep(0.05)
    await settle(state)


@pytest.mark.asyncio
async def test_notifications_while_agents_run_update_one_message_then_one_final(chat):
    state, session, client, bot = chat
    await launch_nine_agents(state, client)
    sent_before = len(bot.sent)

    for n in range(8):
        await feed(state, client, notification_turn(n, f"⏳ Ещё считается: {n + 1} из 9"))

    assert len(bot.sent) == sent_before + 1, "progress must be one message, not one per notification"
    progress_id = len(bot.sent)
    progress_edits = [t for t, kw in bot.edits if kw.get("message_id") == progress_id]
    assert progress_edits[-1].startswith("⏳ Ещё считается: 8 из 9"), "updated in place"
    assert session.background_agents_open() == 1

    await feed(state, client, notification_turn(8, "📊 Итог разбора готов"))

    assert len(bot.sent) == sent_before + 2, "the result comes as a new message"
    assert "Итог разбора" in bot.sent[-1][1]
    assert client.log == []


@pytest.mark.asyncio
async def test_background_agents_tools_do_not_tear_the_text(chat):
    state, session, client, bot = chat
    await launch_nine_agents(state, client)
    sent_before = len(bot.sent)

    await ask(state, "как там экономика?", 2)
    await asyncio.sleep(0.05)
    await feed(state, client, [
        delta("📊 Разбор эк"),
        background_agent_tool("cd /tmp/rlsru && grep price"),
        delta("ономики ещё идёт"),
        background_agent_tool("sed -n 1,40p taxi.lua", agent=1),
        delta(", пришлю выжимку"),
        done(),
    ])

    new = [text for _, text in bot.sent[sent_before:]]
    assert len(new) == 1, f"one answer, no tool bubbles between its pieces: {new}"
    assert "Разбор экономики ещё идёт, пришлю выжимку" in visible_text(bot)
    assert "Bash" not in visible_text(bot)


@pytest.mark.asyncio
async def test_question_merged_into_a_progress_turn_is_answered_as_a_new_message(chat):
    state, session, client, bot = chat
    await launch_nine_agents(state, client)
    await feed(state, client, notification_turn(0, "⏳ Ещё считается: 1 из 9"))
    sent_before = len(bot.sent)
    client.echo_on_query = False

    for frame in notification_turn(1, "⏳ Ещё считается: 2 из 9")[:2]:
        client.events.put_nowait(frame)
    await asyncio.sleep(0.05)
    await ask(state, "А что там с отзывами?", 3)
    await asyncio.sleep(0.05)
    client.take()  # the CLI merges it into the running notification turn
    await feed(state, client, [delta(" Отзывы хорошие"), done()])

    assert len(bot.sent) == sent_before + 1
    assert "Отзывы хорошие" in bot.sent[-1][1], "an answer to the user is pushed, not hidden in an edit"
