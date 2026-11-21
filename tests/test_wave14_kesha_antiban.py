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


def test_quota_gate_antigravity_cooldown_per_pool_and_capped_ttl(tmp_path, monkeypatch):
    cd_file = tmp_path / "cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    assert not hasattr(quota_gate, "ORCHESTRA_DB_PATH")
    assert not hasattr(quota_gate, "_read_orchestra_antigravity_cooldown")

    now = 1_763_622_420.0
    matched, until_ts, reset_s = quota_gate.parse_antigravity_quota_cooldown(
        "Individual quota reached. Resets in 1h56m11s", now=now
    )
    assert matched and reset_s == 6971 and until_ts == now + 900

    m2, u2, r2 = quota_gate.parse_antigravity_quota_cooldown("quota reached 429", now=now)
    assert m2 and r2 == 900 and u2 == now + 900

    info = quota_gate.record_antigravity_cooldown(
        "Individual quota reached. Resets in 20m", now=now, model="gemini-4-neon-medium"
    )
    assert "⛔ Antigravity (gemini) лимит исчерпан — антибан-стоп до " in info["message"]
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-neon-medium", now=now + 5)["until"] == info["until"]
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-argon-high", now=now + 5) is None
    assert quota_gate.antigravity_cooldown_status(model="claude-sonnet-5-5-medium", now=now + 5) is None

    quota_gate.clear_antigravity_cooldown()
    assert quota_gate.antigravity_cooldown_status(model="gemini-4-neon-medium", now=now + 10) is None


@pytest.mark.asyncio
async def test_antigravity_session_cooldown_and_180k_compact_and_cost(tmp_path, monkeypatch):
    cd_file = tmp_path / "cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)

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
    assert config.ANTIGRAVITY_CACHE_TTL_SEC == 1800
    assert config.ANTIGRAVITY_HARD_COMPACT_PCT == 85.0

    # Cold cache triggers compact at 180K; warm/new session stays open until 85%
    import time as _time
    sess._state["lastTurnAt"] = _time.time() - 1900
    sess._state["context"] = {"totalTokens": 180_000, "maxTokens": 1_000_000, "percentage": 18.0}
    res_cold = await sess.check_context_reserve("")
    assert res_cold["ok"] is True and res_cold["should_compact"] is True
    assert res_cold["cache_cold"] is True and res_cold["compact_threshold_tokens"] == 180_000
    res_manual = await sess.check_context_reserve("", manual=True)
    assert res_manual["should_compact"] is False

    # Reset lastTurnAt before verifying cold -> warm cost transition via _record_usage
    sess._state.pop("lastTurnAt", None)
    sess._state["context"] = {"totalTokens": 100_000, "maxTokens": 1_000_000, "percentage": 10.0}
    sess._record_usage({"stopReason": "end_turn"}, output_chars=800, tool_calls=1)
    assert sess.last_response_usage["input_tokens"] == 200_000
    assert sess.last_response_usage["output_tokens"] == 200
    assert sess.last_response_usage["cost_usd"] == pytest.approx(0.4024, abs=1e-6)
    assert sess._state.get("lastTurnAt") is not None

    sess.reset_response_usage()
    sess._record_usage({"stopReason": "end_turn"}, output_chars=400, tool_calls=0)
    assert sess.last_response_usage["input_tokens"] == 0
    assert sess.last_response_usage["cache_read_tokens"] == 100_000
    assert sess.last_response_usage["cost_usd"] == pytest.approx(0.0512, abs=1e-6)


@pytest.mark.asyncio
async def test_antigravity_ttl_dependent_auto_compact_and_persistence(tmp_path, monkeypatch):
    import os
    import time

    cd_file = tmp_path / "cooldown.json"
    monkeypatch.setattr(quota_gate, "ANTIGRAVITY_COOLDOWN_PATH", cd_file)
    quota_gate.clear_antigravity_cooldown()

    now = 1_795_000_000.0
    monkeypatch.setattr(agy.time, "time", lambda: now)

    sess_path = tmp_path / "720740564.antigravity"

    # 1) Warm cache (420s < 1800s) at 185K/1M (18.5%) -> DO NOT compact, ceiling 850K
    sess = build_runtime("antigravity", RuntimeBuildContext(720740564, str(tmp_path), "gemini-4-neon-medium", "", {}, sess_path))
    sess._state.update({
        "sessionId": "sid-warm",
        "context": {"totalTokens": 185_000, "maxTokens": 1_000_000, "percentage": 18.5},
        "lastTurnAt": now - 420,
    })
    res_warm_185 = await sess.check_context_reserve("")
    assert res_warm_185["should_compact"] is False
    assert res_warm_185["cache_cold"] is False
    assert res_warm_185["compact_threshold_tokens"] == 850_000

    # 2) Warm cache (420s < 1800s) at 860K/1M (86%) -> DO compact (85% emergency cap)
    sess._state["context"] = {"totalTokens": 860_000, "maxTokens": 1_000_000, "percentage": 86.0}
    res_warm_860 = await sess.check_context_reserve("")
    assert res_warm_860["should_compact"] is True
    assert res_warm_860["cache_cold"] is False
    assert res_warm_860["compact_threshold_tokens"] == 850_000

    # 3) Cold cache (1900s >= 1800s) at 185K/1M (18.5%) -> DO compact (180K cold threshold)
    sess._state["lastTurnAt"] = now - 1900
    sess._state["context"] = {"totalTokens": 185_000, "maxTokens": 1_000_000, "percentage": 18.5}
    res_cold_185 = await sess.check_context_reserve("")
    assert res_cold_185["should_compact"] is True
    assert res_cold_185["cache_cold"] is True
    assert res_cold_185["compact_threshold_tokens"] == 180_000

    # 4) Cold cache (1900s >= 1800s) at 120K/1M (12%) -> DO NOT compact
    sess._state["context"] = {"totalTokens": 120_000, "maxTokens": 1_000_000, "percentage": 12.0}
    res_cold_120 = await sess.check_context_reserve("")
    assert res_cold_120["should_compact"] is False
    assert res_cold_120["cache_cold"] is True
    assert res_cold_120["compact_threshold_tokens"] == 180_000

    # 5) Persistence across restart from session_file + st_mtime fallback when lastTurnAt absent
    sess._state["context"] = {"totalTokens": 185_000, "maxTokens": 1_000_000, "percentage": 18.5}
    sess._state["lastTurnAt"] = now - 420
    sess._save()
    restarted = build_runtime("antigravity", RuntimeBuildContext(720740564, str(tmp_path), "gemini-4-neon-medium", "", {}, sess_path))
    assert (await restarted.check_context_reserve(""))["cache_cold"] is False

    # Strip lastTurnAt from disk and use st_mtime fallback
    data_no_ts = json.loads(sess_path.read_text())
    data_no_ts.pop("lastTurnAt", None)
    sess_path.write_text(json.dumps(data_no_ts))
    os.utime(sess_path, (now - 300, now - 300))
    from_mtime_warm = build_runtime("antigravity", RuntimeBuildContext(720740564, str(tmp_path), "gemini-4-neon-medium", "", {}, sess_path))
    res_mtime_warm = await from_mtime_warm.check_context_reserve("")
    assert res_mtime_warm["cache_cold"] is False and res_mtime_warm["should_compact"] is False

    os.utime(sess_path, (now - 1900, now - 1900))
    from_mtime_cold = build_runtime("antigravity", RuntimeBuildContext(720740564, str(tmp_path), "gemini-4-neon-medium", "", {}, sess_path))
    res_mtime_cold = await from_mtime_cold.check_context_reserve("")
    assert res_mtime_cold["cache_cold"] is True and res_mtime_cold["should_compact"] is True

    # 6) commit_session_replacement updates lastTurnAt -> cache_cold False afterwards
    tx = restarted.begin_session_replacement()
    restarted.start_session_candidate(tx)
    restarted._state = {"sessionId": "sid-after-compact", "context": {"totalTokens": 185_000, "maxTokens": 1_000_000, "percentage": 18.5}}
    restarted.commit_session_replacement(tx)
    assert restarted._state["lastTurnAt"] == round(now, 3)
    res_post_compact = await restarted.check_context_reserve("")
    assert res_post_compact["cache_cold"] is False and res_post_compact["should_compact"] is False


@pytest.mark.asyncio
async def test_run_on_laptop_requires_telegram_owner(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_CHAT", 720740564)
    monkeypatch.setattr(kesha_tools, "get_current_chat", lambda: 999999)
    res = await kesha_tools.run_on_laptop.handler({"command": "ls"})
    assert res["is_error"] is True and "owner" in res["content"][0]["text"].lower()
