"""V-44: a message typed mid-turn reaches the CLI at once, as in Claude Code.

Owner 14.10.2026: messages sent "мимоходом во время его тулкалов" must arrive
like in Claude Code. The CLI merges a prompt written mid-turn into the running
turn at the next tool boundary, or runs it as the next turn (V-43 research §2,
prompts E and B). Streams use real SDK types through the real ChatState,
ClaudeSession and response_stream; only the CLI and Telegram are faked.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage, ToolResultBlock

from chat_state import ChatPhase, ChatState
from tests.test_background_turns import ask, chat, done, say, settle  # noqa: F401
from tests.test_runtime_limits import visible_text


def bash_call():
    return AssistantMessage(
        content=[ToolUseBlock(id="tu-sleep", name="Bash", input={"command": "sleep 15"})],
        model="claude-opus-5-5",
    )


def bash_done():
    return UserMessage(content=[ToolResultBlock(tool_use_id="tu-sleep", content="fg-done")])


@pytest.fixture
def logged(monkeypatch):
    import message_log

    db = MagicMock()
    monkeypatch.setattr(message_log, "get_db", lambda: db)
    return db


@pytest.mark.asyncio
async def test_message_mid_tool_is_sent_before_result_and_answered(chat, logged):
    state, session, client, bot = chat
    client.echo_on_query = False
    await ask(state, "Запусти скрипт", 1)
    await asyncio.sleep(0.05)
    client.take()
    client.events.put_nowait(bash_call())
    await asyncio.sleep(0.05)

    await ask(state, "и скажи слово BANANA", 2)
    await asyncio.sleep(0.05)
    assert client.queries[-1].endswith("и скажи слово BANANA"), "query must leave before the Result"
    assert not state.deferred

    client.events.put_nowait(bash_done())
    client.take()  # merged into the running turn at the tool boundary
    client.events.put_nowait(say("OK-D\n\nBANANA"))
    client.events.put_nowait(done(turns=2))
    await settle(state)

    shown = visible_text(bot)
    assert "BANANA" in shown and "Пустой ответ" not in shown
    logged_prompts = [call.args[1] for call in logged.log_user.call_args_list]
    assert any(p.endswith("и скажи слово BANANA") for p in logged_prompts), "RAG/history log"
    assert client.log == []


@pytest.mark.asyncio
async def test_steer_queued_by_the_cli_after_our_result_is_still_answered(chat):
    """The CLI took the steer only after our turn's Result: one reply covers both."""
    state, session, client, bot = chat
    client.echo_on_query = False
    await ask(state, "Первый вопрос", 1)
    await asyncio.sleep(0.05)
    client.take()
    client.events.put_nowait(say("Ответ один"))
    await asyncio.sleep(0.05)
    await ask(state, "Второй вопрос", 2)
    await asyncio.sleep(0.05)

    client.events.put_nowait(done())  # our turn ends; the steer is still in the CLI's queue
    await asyncio.sleep(0.05)
    assert state.phase == ChatPhase.PROCESSING, "the steer is owed an answer"
    client.take()
    client.events.put_nowait(say("Ответ два"))
    client.events.put_nowait(done())
    await settle(state)

    shown = visible_text(bot)
    assert "Ответ один" in shown and "Ответ два" in shown
    assert len(client.queries) == 2, "no duplicate send"


@pytest.mark.asyncio
async def test_admission_refusal_keeps_the_message_for_the_next_batch(chat):
    state, session, client, bot = chat
    await ask(state, "Запусти скрипт", 1)
    await asyncio.sleep(0.05)
    client.events.put_nowait(bash_call())

    real_reserve = session.check_context_reserve

    async def no_room(combined="", *, manual=False):
        return {"ok": False, "reason": "session_unavailable"}

    session.check_context_reserve = no_room
    await ask(state, "ещё вопрос", 2)
    await asyncio.sleep(0.05)
    assert len(client.queries) == 1, "refused by admission: nothing written to the CLI"
    assert [[e.prompt for e in b] for b in state.deferred] == [["ещё вопрос"]]

    session.check_context_reserve = real_reserve
    client.events.put_nowait(bash_done())
    client.events.put_nowait(say("Скрипт отработал"))
    client.events.put_nowait(done())
    await asyncio.sleep(0.1)  # the deferred batch is admitted and sent as usual
    assert client.queries[-1].endswith("ещё вопрос")
    client.events.put_nowait(say("Отвечаю на ещё вопрос"))
    client.events.put_nowait(done())
    await settle(state)
    assert "Отвечаю на ещё вопрос" in visible_text(bot)


@pytest.mark.asyncio
async def test_failed_steer_write_falls_back_to_the_queue(chat):
    state, session, client, bot = chat
    await ask(state, "Запусти скрипт", 1)
    await asyncio.sleep(0.05)
    client.events.put_nowait(bash_call())

    async def write_fails(text):
        return False

    session.steer = write_fails
    await ask(state, "потерянное?", 2)
    await asyncio.sleep(0.05)
    assert [[e.prompt for e in b] for b in state.deferred] == [["потерянное?"]]


@pytest.mark.asyncio
async def test_runtime_without_steer_keeps_deferring():
    """Codex declares no steer: arrivals during its turn wait for the next batch."""

    class Codexish:
        session_id = None
        model = "gpt"

    state = ChatState(
        chat_id=1, session=Codexish(), bot=MagicMock(), debounce_sec=0,
        ask_fn=None, set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: ("", [], []), compact_session_fn=None,
        activity_store=MagicMock(), work_dir="/tmp",
    )
    state.phase = ChatPhase.PROCESSING
    from chat_state import PendingEntry

    await state.accept_entry(PendingEntry(prompt="x", message_id=1))
    assert [[e.prompt for e in b] for b in state.deferred] == [["x"]]
    assert state.steering == []
