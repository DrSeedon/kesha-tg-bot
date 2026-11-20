from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

import antigravity_session as agy
import config
import kesha_tools
import model_choices
import quota_gate
from runtime_registry import RuntimeBuildContext, build_runtime


def test_model_choices_ban_xhigh():
    for label, fam, efforts in model_choices.MODELS["antigravity"]:
        assert "xhigh" not in efforts, label
    with pytest.raises(ValueError):
        model_choices.resolve_choice("antigravity", 0, "xhigh")
    assert model_choices.resolve_choice("antigravity", 0, "medium") == ("gemini-4-neon-medium", None)


def test_quota_gate_antigravity_cooldown_and_orchestra_kv(tmp_path, monkeypatch):
    cd_file = tmp_path / "cooldown.json"
    orc_db = tmp_path / "orchestra.db"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    monkeypatch.setattr(quota_gate, "ORCHESTRA_DB_PATH", orc_db)

    now = 1_763_622_420.0
    matched, until_ts, reset_s = quota_gate.parse_antigravity_quota_cooldown(
        "Individual quota reached. Resets in 1h56m11s", now=now
    )
    assert matched and reset_s == 6971 and until_ts == now + 6971 + 90

    m2, u2, r2 = quota_gate.parse_antigravity_quota_cooldown("quota reached 429", now=now)
    assert m2 and r2 == 1800 and u2 == now + 1890

    info = quota_gate.record_antigravity_cooldown("Individual quota reached. Resets in 20m", now=now)
    assert "⛔ Antigravity 5h лимит исчерпан — антибан-стоп до " in info["message"]
    assert quota_gate.antigravity_cooldown_status(now=now + 5)["until"] == info["until"]

    quota_gate.clear_antigravity_cooldown()
    with sqlite3.connect(orc_db) as conn:
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?)",
            ("antigravity_quota_cooldown", json.dumps({"until": now + 3600})),
        )
    st = quota_gate.antigravity_cooldown_status(now=now + 10)
    assert st is not None and st["until"] == now + 3600


@pytest.mark.asyncio
async def test_antigravity_session_cooldown_and_180k_compact_and_cost(tmp_path, monkeypatch):
    cd_file = tmp_path / "cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    monkeypatch.setattr(quota_gate, "ORCHESTRA_DB_PATH", tmp_path / "missing.db")

    sess = build_runtime(
        "antigravity",
        RuntimeBuildContext(123, str(tmp_path), "gemini-4-neon-medium", "", {}, tmp_path / "sess.json"),
    )

    connect_mock = AsyncMock(side_effect=AssertionError("must not connect during cooldown"))
    monkeypatch.setattr(agy.MuxClient, "connect", connect_mock)

    quota_gate.record_antigravity_cooldown("Individual quota reached. Resets in 40m")
    chunks = [
        c async for c in sess.send_message("hello", interactive_user=True, origin="telegram")
    ]
    assert len(chunks) == 1 and chunks[0]["kind"] == "usage_limit"
    assert "антибан-стоп" in chunks[0]["content"]
    connect_mock.assert_not_awaited()

    res_cd = await sess.check_context_reserve("hello")
    assert res_cd["ok"] is False and res_cd["reason"] == "usage_limit"

    quota_gate.clear_antigravity_cooldown()
    assert config.ANTIGRAVITY_AUTO_COMPACT_TOKENS == 180_000
    sess._state["context"] = {"totalTokens": 180_000, "maxTokens": 1_000_000, "percentage": 18.0}
    res_hi = await sess.check_context_reserve("")
    assert res_hi["ok"] is True and res_hi["should_compact"] is True
    res_manual = await sess.check_context_reserve("", manual=True)
    assert res_manual["should_compact"] is False

    sess._state["context"] = {"totalTokens": 100_000, "maxTokens": 1_000_000, "percentage": 10.0}
    sess._record_usage({"stopReason": "end_turn"}, output_chars=800, tool_calls=1)
    assert sess.last_response_usage["input_tokens"] == 200_000
    assert sess.last_response_usage["output_tokens"] == 200
    assert sess.last_response_usage["cost_usd"] == pytest.approx(0.4024, abs=1e-6)

    sess.reset_response_usage()
    sess._record_usage({"stopReason": "end_turn"}, output_chars=400, tool_calls=0)
    assert sess.last_response_usage["input_tokens"] == 0
    assert sess.last_response_usage["cache_read_tokens"] == 100_000
    assert sess.last_response_usage["cost_usd"] == pytest.approx(0.0512, abs=1e-6)


@pytest.mark.asyncio
async def test_run_on_laptop_requires_telegram_owner(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_CHAT", 720740564)
    monkeypatch.setattr(kesha_tools, "get_current_chat", lambda: 999999)
    res = await kesha_tools.run_on_laptop.handler({"command": "ls"})
    assert res["is_error"] is True and "owner" in res["content"][0]["text"].lower()
