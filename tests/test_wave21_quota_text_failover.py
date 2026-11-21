from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import chat_state
import message_log
import quota_gate
import response_stream
from chat_state import ChatState, PendingEntry
from message_log import MessageLog


RAW_AGY_QUOTA = (
    "\u26a0 Individual quota reached. Please upgrade your subscription to increase your limits. "
    "Resets in 119h28m11s."
)


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

    async def send_chat_action(self, *args, **kwargs):
        return True


class FakeMessage:
    def __init__(self, chat_id: int = 720740564):
        self.answers = []
        self.chat = SimpleNamespace(id=chat_id)
        self.chat_id = chat_id
        self.message_id = 10
        self.from_user = SimpleNamespace(id=chat_id, language_code="ru")

    async def answer(self, text, **kwargs):
        message = SimpleNamespace(message_id=42)
        self.answers.append((text, kwargs, message.message_id))
        return message


class FakeRegistry:
    def __init__(self, state):
        self._state = state

    def get(self, _chat_id):
        return self._state


async def _done_typer():
    return None


def _all_visible_texts(bot: FakeBot, msg: FakeMessage) -> list[str]:
    return (
        [t for _, t, _, _ in bot.sent]
        + [t for t, _ in bot.edits]
        + [t for t, _, _ in msg.answers]
    )


def _setup_isolated_state(tmp_path, monkeypatch, *, agy_session, codex_session, existing_codex_selection=None):
    db_path = tmp_path / "chat.db"
    cooldown_path = tmp_path / "agy-cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cooldown_path)
    quota_gate.clear_antigravity_cooldown()

    store = MessageLog(db_path)
    monkeypatch.setattr(message_log, "get_db", lambda: store)

    cid = 720740564
    store.log_user(cid, "Предыдущий вопрос", 1)
    store.log_assistant(cid, "Предыдущий ответ")
    store.select_model(cid, "antigravity", "gemini-4-carbon", "high")
    if existing_codex_selection is not None:
        store.select_model(cid, "codex", *existing_codex_selection)
        # Re-select antigravity as the active runtime while keeping codex's saved selection.
        store.select_model(cid, "antigravity", "gemini-4-carbon", "high")

    bot = FakeBot()
    built = []

    def build_runtime(target: str, chat_id: int, selection=None):
        built.append((target, chat_id, selection))
        assert target == "codex"
        return codex_session

    async def fake_ask(*_args, **_kwargs):
        return False

    state = ChatState(
        chat_id=cid,
        session=agy_session,
        bot=bot,
        debounce_sec=0,
        ask_fn=fake_ask,
        set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: ("", [], []),
        compact_session_fn=AsyncMock(return_value={"ok": True}),
        activity_store=store,
        work_dir=str(tmp_path),
        runtime_id="antigravity",
        build_runtime_fn=build_runtime,
    )
    return cid, store, bot, state, built, cooldown_path


class _AgyScriptedSession:
    def __init__(self, events_or_exc, *, tools_model="gemini-4-carbon"):
        self.model = tools_model
        self.reasoning_effort = "high"
        self.calls = []
        self.disconnected = False
        self._events_or_exc = events_or_exc

    def set_model(self, model, reasoning_effort=None):
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def check_context_reserve(self, _prompt):
        return {"ok": True}

    async def ping(self):
        return True

    async def probe_readiness(self):
        return {"ok": True}

    async def safe_disconnect(self):
        self.disconnected = True

    def reconnect(self):
        return None

    async def send_message(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if isinstance(self._events_or_exc, Exception):
            raise self._events_or_exc
        for item in self._events_or_exc:
            if isinstance(item, Exception):
                raise item
            yield item


class _CodexScriptedSession:
    def __init__(self, reply_text: str = "Готово, ответил из Codex"):
        self.model = "gpt-6.2-sol"
        self.reasoning_effort = "high"
        self.calls = []
        self.reply_text = reply_text

    async def check_context_reserve(self, _prompt):
        return {"ok": True}

    async def ping(self):
        return True

    async def probe_readiness(self):
        return {"ok": True}

    async def safe_disconnect(self):
        return None

    def reconnect(self):
        return None

    async def send_message(self, text: str):
        # Strictly mirrors real CodexSession.send_message(self, text: str) —
        # must fail loudly if Antigravity-only kwargs leak through send_options.
        self.calls.append(text)
        yield {"type": "text_delta", "content": self.reply_text}
        yield {"type": "turn_done"}


def test_quota_text_detector_unit():
    matched, until_ts, reset_sec = quota_gate.looks_like_antigravity_quota_text(
        RAW_AGY_QUOTA, runtime="antigravity", tools=0
    )
    assert matched is True
    assert until_ts is not None
    assert reset_sec is not None and reset_sec > 119 * 3600

    assert quota_gate.looks_like_antigravity_quota_prefix(
        "\u26a0 Individual ", runtime="antigravity", tools=0
    ) is True

    # Must NOT trigger when tools were invoked, non-antigravity runtime, or long technical prose.
    assert quota_gate.looks_like_antigravity_quota_text(
        RAW_AGY_QUOTA, runtime="antigravity", tools=1
    ) == (False, None, None)
    assert quota_gate.looks_like_antigravity_quota_text(
        RAW_AGY_QUOTA, runtime="codex", tools=0
    ) == (False, None, None)
    assert quota_gate.looks_like_antigravity_quota_text(
        "Ошибка HTTP 429 возникает при слишком частых запросах к API.",
        runtime="antigravity",
        tools=0,
    ) == (False, None, None)
    long_doc = RAW_AGY_QUOTA + "\n" + ("Подробный разбор архитектуры. " * 30)
    assert quota_gate.looks_like_antigravity_quota_text(
        long_doc, runtime="antigravity", tools=0
    ) == (False, None, None)


@pytest.mark.asyncio
async def test_text_chunk_quota_triggers_cooldown_failover_and_codex_reply(tmp_path, monkeypatch):
    agy = _AgyScriptedSession([{"type": "text", "content": RAW_AGY_QUOTA}])
    codex = _CodexScriptedSession("Ответ от Sol 6.2 без утечки")
    cid, store, bot, state, built, _ = _setup_isolated_state(
        tmp_path, monkeypatch, agy_session=agy, codex_session=codex
    )
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer

    result = await response_stream._ask_inner(
        msg,
        "Проверь статус",
        cid,
        typer,
        interactive_user=True,
        origin="tg",
        reminder="check",
        audio_paths=["/tmp/voice.ogg"],
    )

    assert result is True
    assert state.runtime_id == "codex"
    assert state.session is codex
    assert agy.disconnected is True
    assert built == [("codex", cid, ("gpt-6.2-sol", "high"))]
    assert quota_gate.antigravity_cooldown_status() is not None
    assert store.get_runtime(cid) == "codex"
    assert store.get_model_selection(cid, "codex") == ("gpt-6.2-sol", "high")
    # Handoff was consumed into the replayed Codex prompt, not left stale in DB.
    assert store.pending_handoff(cid, "codex") is None
    assert len(codex.calls) == 1
    assert "Предыдущий вопрос" in codex.calls[0]
    assert "Проверь статус" in codex.calls[0]
    visible = _all_visible_texts(bot, msg)
    assert any("Ответ от Sol 6.2 без утечки" in t for t in visible)
    assert not any("Individual quota reached" in t for t in visible)


@pytest.mark.asyncio
async def test_split_text_delta_quota_buffers_prefix_preserves_effort_and_deletes_draft(tmp_path, monkeypatch):
    agy = _AgyScriptedSession(
        [
            {"type": "text_delta", "content": "\u26a0 Individual "},
            {"type": "text_delta", "content": "quota reached. Please upgrade your subscription to increase your limits. "},
            {"type": "text_delta", "content": "Resets in 119h28m11s."},
            {"type": "turn_done"},
        ]
    )
    codex = _CodexScriptedSession("Чистый ответ Codex")
    cid, store, bot, state, built, _ = _setup_isolated_state(
        tmp_path,
        monkeypatch,
        agy_session=agy,
        codex_session=codex,
        existing_codex_selection=("gpt-5.4", "medium"),
    )
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer

    result = await response_stream._ask_inner(msg, "Привет", cid, typer)

    assert result is True
    assert built == [("codex", cid, ("gpt-6.2-sol", "medium"))]
    assert store.get_model_selection(cid, "codex") == ("gpt-6.2-sol", "medium")
    assert quota_gate.antigravity_cooldown_status() is not None
    assert len(codex.calls) == 1
    visible = _all_visible_texts(bot, msg)
    assert visible == ["Чистый ответ Codex"]
    assert not bot.deleted


@pytest.mark.asyncio
async def test_non_prefix_partial_draft_is_deleted_when_quota_text_completes(tmp_path, monkeypatch):
    agy = _AgyScriptedSession(
        [
            {"type": "text_delta", "content": "Checking... "},
            {"type": "text_delta", "content": RAW_AGY_QUOTA},
            {"type": "turn_done"},
        ]
    )
    codex = _CodexScriptedSession("Ответ после удаления черновика")
    cid, _, bot, state, _, _ = _setup_isolated_state(
        tmp_path, monkeypatch, agy_session=agy, codex_session=codex
    )
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer

    result = await response_stream._ask_inner(msg, "Запрос", cid, typer)

    assert result is True
    assert len(bot.deleted) >= 1
    assert any("Ответ после удаления черновика" in t for t in _all_visible_texts(bot, msg))
    assert not any("Individual quota reached" in t for t in _all_visible_texts(bot, msg))


@pytest.mark.asyncio
async def test_no_false_positive_with_tools_streams_normally(tmp_path, monkeypatch):
    agy = _AgyScriptedSession(
        [
            {"type": "tool", "name": "bash", "input": {"command": "echo hi"}}, {"type": "tool_done"},
            {"type": "text", "content": RAW_AGY_QUOTA},
            {"type": "turn_done"},
        ]
    )
    codex = _CodexScriptedSession("should not run")
    cid, _, bot, state, built, _ = _setup_isolated_state(
        tmp_path, monkeypatch, agy_session=agy, codex_session=codex
    )
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer

    await response_stream._ask_inner(msg, "Покажи пример ошибки квоты", cid, typer)

    assert state.runtime_id == "antigravity"
    assert built == []
    assert codex.calls == []
    assert quota_gate.antigravity_cooldown_status() is None
    assert any("Individual quota reached" in t for t in _all_visible_texts(bot, msg))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error_chunk", "exception"])
async def test_error_chunk_and_exception_quota_failover_to_codex(tmp_path, monkeypatch, mode):
    if mode == "error_chunk":
        agy = _AgyScriptedSession(
            [{"type": "error", "kind": "usage_limit", "content": RAW_AGY_QUOTA}]
        )
    else:
        agy = _AgyScriptedSession(RuntimeError(RAW_AGY_QUOTA))
    codex = _CodexScriptedSession(f"Восстановлено из {mode}")
    cid, _, bot, state, _, _ = _setup_isolated_state(
        tmp_path, monkeypatch, agy_session=agy, codex_session=codex
    )
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer

    result = await response_stream._ask_inner(msg, "Вопрос", cid, typer)

    assert result is True
    assert state.runtime_id == "codex"
    assert quota_gate.antigravity_cooldown_status() is not None
    assert len(codex.calls) == 1
    visible = _all_visible_texts(bot, msg)
    assert any(f"Восстановлено из {mode}" in t for t in visible)
    assert not any("Лимит запросов исчерпан" in t for t in visible)


@pytest.mark.asyncio
async def test_preturn_active_cooldown_failovers_before_querying_antigravity(tmp_path, monkeypatch):
    agy = _AgyScriptedSession([{"type": "text", "content": "never reached"}])
    codex = _CodexScriptedSession("Сразу через Codex")
    cid, _, bot, state, _, _ = _setup_isolated_state(
        tmp_path, monkeypatch, agy_session=agy, codex_session=codex
    )
    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA)
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer

    result = await response_stream._ask_inner(msg, "Прямой вопрос", cid, typer)

    assert result is True
    assert agy.calls == []
    assert state.runtime_id == "codex"
    assert len(codex.calls) == 1
    assert any("Сразу через Codex" in t for t in _all_visible_texts(bot, msg))


@pytest.mark.asyncio
async def test_run_batch_preturn_cooldown_auto_failovers_to_codex(tmp_path, monkeypatch):
    agy = _AgyScriptedSession([{"type": "text", "content": "never reached"}])
    codex = _CodexScriptedSession("ok")
    cid, store, bot, state, built, _ = _setup_isolated_state(
        tmp_path, monkeypatch, agy_session=agy, codex_session=codex
    )
    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA)

    asked = []

    async def fake_ask(_msg, prompt, _cid, **_kw):
        asked.append((state.runtime_id, getattr(state.session, "model", None), prompt))
        return False

    state._ask_fn = fake_ask
    batch = [PendingEntry(prompt="[user]: сделай задачу", message_id=2, message=None, interactive_user=True)]
    await state._run_batch(batch)

    assert state.runtime_id == "codex"
    assert state.session is codex
    assert built == [("codex", cid, ("gpt-6.2-sol", "high"))]
    assert len(asked) == 1
    assert asked[0][0] == "codex"
    assert asked[0][1] == "gpt-6.2-sol"
    assert "Предыдущий вопрос" in asked[0][2]
    assert "сделай задачу" in asked[0][2]
    assert store.pending_handoff(cid, "codex") is None
