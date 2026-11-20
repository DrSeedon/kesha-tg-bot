from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers


class FakeState:
    def __init__(self, runtime_id="claude"):
        self.runtime_id = runtime_id
        self.session = SimpleNamespace(model="model", rate_limit=None)
        self.is_busy = False
        self.calls = []

    async def switch_runtime(self, target):
        self.calls.append(target)
        if target == self.runtime_id:
            return {"ok": False, "reason": "same", "runtime": target}
        previous = self.runtime_id
        self.runtime_id = target
        return {
            "ok": True,
            "runtime": target,
            "previous": previous,
            "model": "model",
            "handoff": None,
            "handoff_status": "unsupported",
        }


def source(text="", uid=1, chat_id=42):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=uid, language_code="ru"),
        chat=SimpleNamespace(id=chat_id),
        answer=AsyncMock(),
    )


def test_runtime_keyboard_marks_the_current_backend():
    keyboard = handlers._runtime_keyboard(SimpleNamespace(runtime_id="antigravity", session=SimpleNamespace(model="claude-sonnet-5-5-medium")))

    row0 = keyboard.inline_keyboard[0]
    row1 = keyboard.inline_keyboard[1]
    row2 = keyboard.inline_keyboard[2]
    assert row0[0].callback_data == "runtime:gemini"
    assert row0[1].callback_data == "runtime:argon"
    assert row1[0].text.startswith("✅ ")
    assert row1[0].callback_data == "runtime:claude"
    assert row1[1].callback_data == "runtime:opus"
    assert row2[0].callback_data == "runtime:codex"


def test_direct_runtime_commands_are_published():
    commands = {command.command for command in handlers.COMMANDS_RU}
    assert {"runtime", "claude", "codex"} <= commands


@pytest.mark.asyncio
async def test_codex_command_switches_without_runtime_argument(monkeypatch):
    state = FakeState()
    monkeypatch.setattr(handlers, "_registry", SimpleNamespace(get=lambda _cid: state))
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    msg = source("/codex")

    await handlers.h_codex(msg)

    assert state.calls == ["codex"]
    assert "claude" in msg.answer.await_args.args[0]
    assert "codex" in msg.answer.await_args.args[0]
    assert msg.answer.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_callback_is_acknowledged_before_slow_switch(monkeypatch):
    order = []
    state = FakeState()

    async def switch(target):
        order.append("switch")
        return await FakeState.switch_runtime(state, target)

    async def acknowledge(*args, **kwargs):
        order.append("ack")

    state.switch_runtime = switch
    message = source(chat_id=42)
    message.edit_text = AsyncMock()
    query = SimpleNamespace(
        data="runtime:codex",
        from_user=SimpleNamespace(id=1, language_code="ru"),
        message=message,
        answer=acknowledge,
    )
    monkeypatch.setattr(handlers, "_registry", SimpleNamespace(get=lambda _cid: state))
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    await handlers.h_runtime_callback(query)

    assert order == ["ack", "switch"]
    message.edit_text.assert_awaited_once()
    markup = message.edit_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[2][0].text == "✅ Codex (Sol 6.2)"
