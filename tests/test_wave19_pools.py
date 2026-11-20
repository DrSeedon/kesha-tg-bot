"""Wave 19 Kesha tests: all 3 Antigravity pools, fast commands, intra-runtime switching & Deepgram gate."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import handlers
import model_choices
from chat_state import ChatState
from message_log import MessageLog


def _msg(text="/runtime", uid=720740564, chat_id=720740564):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=uid, is_bot=False, first_name="Максим", last_name=None, username="maxim", language_code="ru"),
        chat=SimpleNamespace(id=chat_id),
        caption=None,
        date=datetime.now(timezone.utc),
        message_id=10,
        answer=AsyncMock(),
    )


def test_model_choices_cover_all_3_pools_and_codex_without_xhigh():
    assert model_choices.resolve_choice("antigravity", 0, "medium") == ("gemini-4-neon-medium", None)
    assert model_choices.resolve_choice("antigravity", 4, "medium") == ("gemini-4-argon-medium", None)
    assert model_choices.resolve_choice("antigravity", 6, "medium") == ("claude-sonnet-5-5-medium", None)
    assert model_choices.resolve_choice("antigravity", 7, "medium") == ("claude-opus-5-5-medium", None)
    assert model_choices.resolve_choice("antigravity", 8, "medium") == ("gpt-oss-120b-medium", None)
    assert model_choices.resolve_choice("codex", 0, "medium") == ("gpt-6.2-sol", "medium")
    assert model_choices.resolve_choice("codex", 1, "high") == ("gpt-6.2-astra", "high")
    for rt, families in model_choices.MODELS.items():
        for label, _, efforts in families:
            assert "xhigh" not in efforts, f"{rt}/{label} must not expose xhigh"
    assert config.MODEL == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_fast_commands_switch_pools_and_presets(monkeypatch):
    calls = []

    class FakeState:
        def __init__(self):
            self.runtime_id = "antigravity"
            self.session = SimpleNamespace(model="gemini-4-neon-medium", rate_limit=None)

        async def switch_runtime(self, target, **kwargs):
            calls.append((target, kwargs.get("selection")))
            sel = kwargs.get("selection")
            if target == self.runtime_id and sel == (self.session.model, None):
                return {"ok": False, "reason": "same", "runtime": target}
            prev = self.runtime_id
            self.runtime_id = target
            if sel:
                self.session.model = sel[0]
            return {
                "ok": True,
                "runtime": target,
                "previous": prev,
                "model": self.session.model,
                "handoff": None,
                "handoff_status": "same_runtime" if prev == target else "carried",
            }

    state = FakeState()
    monkeypatch.setattr(handlers, "_registry", SimpleNamespace(get=lambda _cid: state))
    monkeypatch.setattr(handlers, "ALLOWED", {720740564})

    await handlers.h_argon(_msg("/argon"))
    await handlers.h_claude(_msg("/claude"))
    await handlers.h_sonnet(_msg("/sonnet"))  # same claude-sonnet-5-5-medium
    await handlers.h_opus(_msg("/opus"))
    await handlers.h_gemini(_msg("/gemini"))
    await handlers.h_codex(_msg("/codex"))

    assert calls == [
        ("antigravity", ("gemini-4-argon-medium", None)),
        ("antigravity", ("claude-sonnet-5-5-medium", None)),
        ("antigravity", ("claude-sonnet-5-5-medium", None)),
        ("antigravity", ("claude-opus-5-5-medium", None)),
        ("antigravity", ("gemini-4-neon-medium", None)),
        ("codex", None),
    ]
    published = {c.command for c in handlers.COMMANDS_RU}
    assert {"gemini", "argon", "claude", "sonnet", "opus", "codex", "model", "runtime"} <= published


@pytest.mark.asyncio
async def test_intra_antigravity_switch_preserves_session_and_persists_db(tmp_path):
    store = MessageLog(tmp_path / "messages.db")
    sess_file = tmp_path / "720740564.antigravity"
    sess_file.write_text(json.dumps({"sessionId": "keep-sid-1", "token": "tok"}))
    sess = SimpleNamespace(
        model="gemini-4-neon-medium",
        reasoning_effort=None,
        session_file=sess_file,
        rate_limit=None,
        safe_disconnect=AsyncMock(),
    )
    chat = ChatState(
        chat_id=720740564,
        session=sess,
        bot=SimpleNamespace(),
        debounce_sec=0,
        ask_fn=AsyncMock(),
        set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: "",
        compact_session_fn=AsyncMock(return_value={"ok": True}),
        activity_store=store,
        work_dir=str(tmp_path),
        runtime_id="antigravity",
        build_runtime_fn=lambda *_a, **_kw: pytest.fail("must not rebuild session on intra-agy switch"),
    )

    res1 = await chat.switch_runtime("antigravity", selection=("claude-sonnet-5-5-medium", None))
    assert res1["ok"] is True
    assert res1["handoff_status"] == "same_runtime"
    assert chat.session.model == "claude-sonnet-5-5-medium"
    assert store.get_model_selection(720740564, "antigravity") == ("claude-sonnet-5-5-medium", None)
    assert json.loads(sess_file.read_text())["sessionId"] == "keep-sid-1"
    sess.safe_disconnect.assert_not_awaited()

    res_same = await chat.switch_runtime("antigravity", selection=("claude-sonnet-5-5-medium", None))
    assert res_same == {"ok": False, "reason": "same", "runtime": "antigravity"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-sonnet-5-5-medium", "claude-opus-5-5-medium", "gpt-oss-120b-medium"])
async def test_claude_3p_and_gptoss_voice_uses_deepgram_not_native_ogg(tmp_path, monkeypatch, model):
    import media

    uid = 720740564
    monkeypatch.setattr(handlers, "ALLOWED", {uid})
    monkeypatch.setattr(config, "DEEPGRAM", "dg-test-key")
    monkeypatch.setattr(handlers, "_bot", SimpleNamespace(send_chat_action=AsyncMock()))
    transcribe = AsyncMock(return_value=("привет кеша", None))
    monkeypatch.setattr(handlers, "transcribe", transcribe)
    media_finished = AsyncMock()
    start = SimpleNamespace(
        runtime_id="antigravity",
        session=SimpleNamespace(model=model),
        media_finished=media_finished,
    )
    monkeypatch.setattr(handlers, "_start_media", AsyncMock(return_value=(start, 0, 0)))
    download = AsyncMock(return_value=str(tmp_path / "voice.oga"))
    monkeypatch.setattr(handlers, "download_file", download)
    monkeypatch.setattr(media, "native_ogg", AsyncMock(side_effect=AssertionError("native_ogg called")))

    msg = _msg(uid=uid, chat_id=uid)
    msg.voice = SimpleNamespace(file_id="fid", file_unique_id="fuid", duration=4)
    msg.forward_date = None
    msg.forward_from = None
    msg.forward_from_chat = None
    msg.forward_sender_name = None
    msg.forward_origin = None
    msg.reply_to_message = None

    await handlers.h_voice(msg)
    transcribe.assert_awaited_once()
    entry = media_finished.await_args.args[0]
    assert "привет кеша" in entry.prompt
    assert not getattr(entry, "audio_paths", ())
