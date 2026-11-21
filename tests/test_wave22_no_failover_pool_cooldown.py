from __future__ import annotations

import ast
import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import antigravity_session as agy
import message_log
import quota_gate
import response_stream
from chat_state import ChatState, PendingEntry
from message_log import MessageLog
from runtime_registry import RuntimeBuildContext, build_runtime


RAW_AGY_QUOTA = (
    "\u26a0 Individual quota reached. Please upgrade your subscription to increase your limits. "
    "Resets in 119h28m11s."
)
ROOT = Path(__file__).resolve().parents[1]


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []
        self.next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        msg = SimpleNamespace(message_id=self.next_id)
        self.sent.append((chat_id, text, kwargs, msg.message_id))
        return msg

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
        msg = SimpleNamespace(message_id=42)
        self.answers.append((text, kwargs, msg.message_id))
        return msg


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


def test_no_auto_failover_symbols_and_zero_orchestra_coupling():
    assert not hasattr(ChatState, "failover_to_codex")
    assert "_try_agy_failover" not in Path(response_stream.__file__).read_text(encoding="utf-8")
    assert not hasattr(quota_gate, "ORCHESTRA_DB_PATH")
    assert not hasattr(quota_gate, "_read_orchestra_antigravity_cooldown")

    # Default cooldown path must be inside kesha storage, never /tmp
    src_qg = Path(quota_gate.__file__).read_text(encoding="utf-8")
    assert "/opt/kesha-bot/storage/antigravity-cooldown.json" in src_qg
    assert "/tmp/kesha-antigravity-cooldown.json" not in src_qg

    for mod_name in ("quota_gate.py", "antigravity_session.py", "chat_state.py", "response_stream.py"):
        text = (ROOT / mod_name).read_text(encoding="utf-8")
        assert "/home/kesha/orchestra" not in text, f"{mod_name} must not reference /home/kesha/orchestra"

    for py_file in sorted(ROOT.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.") and alias.name != "app"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("app.") and mod != "app"


def test_per_pool_cooldown_ttl_cap_900_and_0600_permissions(tmp_path, monkeypatch):
    cd_file = tmp_path / "storage" / "antigravity-cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    now = 1_795_000_000.0

    # Record 119h28m11s exhaustion on 3P pool (claude-sonnet-5-5-medium)
    info = quota_gate.record_antigravity_cooldown(
        RAW_AGY_QUOTA, now=now, model="claude-sonnet-5-5-medium"
    )
    assert info["pool"] == "3p"
    assert info["until"] == now + 900  # hard capped at 15 min
    assert cd_file.is_file()
    assert stat.S_IMODE(cd_file.stat().st_mode) == 0o600

    # 3P cooldown must NOT block Gemini (neon/hydrogen/carbon) or Argon pools!
    assert quota_gate.antigravity_cooldown_status(model="claude-opus-5-5-high", now=now + 10) is not None
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-neon-medium", now=now + 10) is None
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-argon-high", now=now + 10) is None

    # After 901s, 3P cooldown expires automatically
    assert quota_gate.antigravity_cooldown_status(model="claude-sonnet-5-5-medium", now=now + 901) is None


@pytest.mark.asyncio
async def test_3p_cooldown_does_not_block_gemini_in_antigravity_session(tmp_path, monkeypatch):
    cd_file = tmp_path / "antigravity-cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA, model="claude-sonnet-5-5-medium")

    neon_sess = build_runtime(
        "antigravity",
        RuntimeBuildContext(720740564, str(tmp_path), "gemini-4-neon-medium", "", {}, tmp_path / "neon.json"),
    )
    res_neon = await neon_sess.check_context_reserve("hello")
    assert res_neon["ok"] is True

    sonnet_sess = build_runtime(
        "antigravity",
        RuntimeBuildContext(720740564, str(tmp_path), "claude-sonnet-5-5-medium", "", {}, tmp_path / "sonnet.json"),
    )
    res_sonnet = await sonnet_sess.check_context_reserve("hello")
    assert res_sonnet["ok"] is False and res_sonnet["reason"] == "usage_limit"


@pytest.mark.asyncio
async def test_runtime_never_switches_on_429_cooldown_or_quota_error(tmp_path, monkeypatch):
    cd_file = tmp_path / "antigravity-cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)

    store = MessageLog(tmp_path / "chat.db")
    monkeypatch.setattr(message_log, "get_db", lambda: store)
    cid = 720740564
    store.select_model(cid, "antigravity", "gemini-4-neon-medium", None)

    class FailingAgySession:
        model = "gemini-4-neon-medium"
        reasoning_effort = None
        async def check_context_reserve(self, _p):
            return {"ok": False, "reason": "usage_limit", "message": "limit"}
        async def send_message(self, _p, **_kw):
            yield {"type": "error", "kind": "usage_limit", "content": RAW_AGY_QUOTA}
        def reconnect(self):
            return None

    bot = FakeBot()
    agy_sess = FailingAgySession()
    state = ChatState(
        chat_id=cid,
        session=agy_sess,
        bot=bot,
        debounce_sec=0,
        ask_fn=AsyncMock(return_value=False),
        set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: ("", [], []),
        compact_session_fn=AsyncMock(return_value={"ok": True}),
        activity_store=store,
        work_dir=str(tmp_path),
        runtime_id="antigravity",
        build_runtime_fn=lambda target, _cid, selection=None: pytest.fail(f"must never auto-switch to {target}"),
    )

    # 1) Even with active gemini cooldown + usage_limit reserve, _run_batch stays on antigravity
    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA, model="gemini-4-neon-medium")
    await state._run_batch([PendingEntry(prompt="привет", message_id=1, message=None, interactive_user=True)])
    assert state.runtime_id == "antigravity"
    assert store.get_runtime(cid) == "antigravity"
    assert len(bot.sent) >= 1

    # 2) In _ask_inner, usage_limit error shows honest message and stays on antigravity
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(state))
    msg = FakeMessage(cid)
    typer = asyncio.create_task(_done_typer())
    await typer
    await response_stream._ask_inner(msg, "тест", cid, typer, interactive_user=True, origin="telegram")
    assert state.runtime_id == "antigravity"
    assert store.get_runtime(cid) == "antigravity"
    visible = _all_visible_texts(bot, msg)
    assert any("лимит" in t.casefold() or "quota" in t.casefold() for t in visible)


@pytest.mark.asyncio
async def test_manual_switch_runtime_to_antigravity_clears_cooldown(tmp_path, monkeypatch):
    cd_file = tmp_path / "antigravity-cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA, model="gemini-4-neon-medium")
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-neon-medium") is not None

    store = MessageLog(tmp_path / "chat.db")
    monkeypatch.setattr(message_log, "get_db", lambda: store)
    cid = 720740564
    store.select_model(cid, "codex", "gpt-6.2-sol", "high")

    class FakeCodex:
        model = "gpt-6.2-sol"
        reasoning_effort = "high"
        async def safe_disconnect(self):
            return None

    class FakeNewAgy:
        model = "gemini-4-neon-medium"
        reasoning_effort = None
        async def probe_readiness(self):
            return {"ok": True}
        async def safe_disconnect(self):
            return None

    state = ChatState(
        chat_id=cid,
        session=FakeCodex(),
        bot=FakeBot(),
        debounce_sec=0,
        ask_fn=AsyncMock(return_value=True),
        set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: ("", [], []),
        compact_session_fn=AsyncMock(return_value={"ok": True}),
        activity_store=store,
        work_dir=str(tmp_path),
        runtime_id="codex",
        build_runtime_fn=lambda target, _cid, selection=None: FakeNewAgy(),
    )

    res = await state.switch_runtime("antigravity", selection=("gemini-4-neon-medium", None))
    assert res["ok"] is True
    assert state.runtime_id == "antigravity"
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-neon-medium") is None

    # Also test intra-runtime switch and "same" branch clear cooldown
    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA, model="gemini-4-neon-medium")
    res_intra = await state.switch_runtime("antigravity", selection=("gemini-4-argon-high", None))
    assert res_intra["ok"] is True
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-neon-medium") is None

    quota_gate.record_antigravity_cooldown(RAW_AGY_QUOTA, model="gemini-4-argon-high")
    res_same = await state.switch_runtime("antigravity", selection=("gemini-4-argon-high", None))
    assert res_same["reason"] == "same"
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-argon-high") is None
