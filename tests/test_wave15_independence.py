from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import antigravity_session as agy
import config
import handlers
import inbox_server
import kesha_tools as kt
import orch_ops
from chat_state import ChatState, ChatPhase
from message_log import MessageLog

ROOT = Path(__file__).resolve().parents[1]


def test_ast_zero_orchestra_imports_or_acp_mux_exec():
    """Every *.py in Kesha must have 0 imports of app.* and 0 app.acp_mux process launches."""
    for py_file in sorted(ROOT.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app."), f"{py_file.name}: forbidden import {alias.name}"
                    assert alias.name != "app", f"{py_file.name}: forbidden import app"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("app.") and mod != "app", f"{py_file.name}: forbidden from {mod}"
        assert "app.acp_mux" not in source, f"{py_file.name}: app.acp_mux reference still present"
        assert "_orchestra_python" not in source, f"{py_file.name}: _orchestra_python reference still present"
    assert not hasattr(agy.MuxClient, "_fallback")


def test_allowed_is_fail_closed_and_owner_gate(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED", set())
    assert handlers.allowed(720740564) is False

    monkeypatch.setattr(handlers, "ALLOWED", {720740564, 893553748})
    monkeypatch.setattr(handlers, "OWNER_ID", 720740564)
    assert handlers.allowed(893553748) is True
    assert handlers.is_owner(720740564) is True
    assert handlers.is_owner(893553748) is False


@pytest.mark.asyncio
async def test_non_owner_cannot_run_orch_or_restart(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "ALLOWED", {720740564, 893553748})
    monkeypatch.setattr(handlers, "OWNER_ID", 720740564)
    monkeypatch.setattr(handlers, "_send_safe", AsyncMock(side_effect=lambda _m, txt, **kw: sent.append(txt)))
    exec_mock = AsyncMock(side_effect=AssertionError("must not spawn subprocess for non-owner"))
    monkeypatch.setattr(handlers.asyncio, "create_subprocess_exec", exec_mock)

    katya_msg = lambda text: SimpleNamespace(
        from_user=SimpleNamespace(id=893553748, language_code="ru"),
        chat=SimpleNamespace(id=893553748),
        text=text,
    )
    await handlers.h_restart(katya_msg("/restart"))
    await handlers.h_orch(katya_msg("/orch status"))
    assert len(sent) == 2
    assert all("только владельцу" in s for s in sent)
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbox_server_requires_bearer_token(monkeypatch):
    monkeypatch.setattr(inbox_server, "ALLOWED", {720740564})
    monkeypatch.setattr(inbox_server, "_expected_inbox_token", lambda: "super-secret-token")
    monkeypatch.setattr(inbox_server, "_bot_ref", AsyncMock())
    monkeypatch.setattr(inbox_server, "_registry_ref", SimpleNamespace(get=lambda _: SimpleNamespace(runtime_id="codex", accept_entry=AsyncMock())))

    no_hdr = SimpleNamespace(headers={}, json=AsyncMock(return_value={"chat_id": 720740564, "message": "hi"}))
    r1 = await inbox_server.handle_inbox(no_hdr)
    assert r1.status == 401

    bad_hdr = SimpleNamespace(headers={"Authorization": "Bearer wrong-token"}, json=AsyncMock(return_value={"chat_id": 720740564, "message": "hi"}))
    r2 = await inbox_server.handle_inbox(bad_hdr)
    assert r2.status == 403

    ok_hdr = SimpleNamespace(headers={"Authorization": "Bearer super-secret-token"}, json=AsyncMock(return_value={"chat_id": 720740564, "message": "hi"}))
    r3 = await inbox_server.handle_inbox(ok_hdr)
    assert r3.status == 200


@pytest.mark.asyncio
async def test_antigravity_offline_mux_fails_fast_and_auto_failovers_to_codex_sol62(tmp_path, monkeypatch):
    monkeypatch.setattr(agy, "MUX_PATH", tmp_path / "missing.acp-mux.sock")
    monkeypatch.setattr(agy, "ACP_CONNECT_TIMEOUT_SEC", 0.05)

    with pytest.raises(agy.MuxError, match="ACP mux offline"):
        await agy.MuxClient.connect()

    store = MessageLog(tmp_path / "chat.db")
    store.log_user(720740564, "Предыдущий вопрос", 1)
    store.log_assistant(720740564, "Предыдущий ответ")
    store.select_model(720740564, "antigravity", "gemini-4-neon-medium", None)

    import message_log
    monkeypatch.setattr(message_log, "get_db", lambda: store)

    class AntigravitySession:
        model = "gemini-4-neon-medium"
        def __init__(self):
            self.safe_disconnect = AsyncMock()
            self.check_context_reserve = AsyncMock(return_value={"ok": True, "should_compact": False})

    bot = AsyncMock()
    agy_sess = AntigravitySession()
    codex_sess = SimpleNamespace(
        model="gpt-6.2-sol",
        probe_readiness=AsyncMock(return_value={"ok": True}),
        inject_context=AsyncMock(),
        safe_disconnect=AsyncMock(),
        check_context_reserve=AsyncMock(return_value={"ok": True, "should_compact": False}),
    )
    asked = []

    async def fake_ask(msg, prompt, cid, **kwargs):
        asked.append((state.runtime_id, getattr(state.session, "model", None), prompt))
        return True

    state = ChatState(
        chat_id=720740564,
        session=agy_sess,
        bot=bot,
        debounce_sec=0,
        ask_fn=fake_ask,
        set_current_chat_fn=lambda _c: None,
        get_lazy_block_fn=lambda _c: "",
        compact_session_fn=AsyncMock(return_value={"ok": True}),
        activity_store=store,
        work_dir=str(tmp_path),
        runtime_id="antigravity",
        build_runtime_fn=lambda target, _cid, selection=None: codex_sess,
    )

    from chat_state import PendingEntry
    batch = [PendingEntry(prompt="[user]: сделай дело", message_id=2, message=None, interactive_user=True)]
    monkeypatch.setattr(agy, "is_acp_mux_socket_present", lambda: False)
    state._check_batch_context = AsyncMock(return_value={"ok": True, "should_compact": False})
    await state._run_batch(batch)

    assert state.runtime_id == "codex"
    assert state.session is codex_sess
    assert store.get_runtime(720740564) == "codex"
    assert store.get_model_selection(720740564, "codex") == ("gpt-6.2-sol", "medium")
    handoff = store.pending_handoff(720740564, "codex")
    assert handoff is not None and "Предыдущий вопрос" in handoff[1]
    assert any("⚡ Antigravity недоступен — переключил чат на GPT-6.2-Sol" in str(c) for c in bot.send_message.call_args_list)
    assert len(asked) == 1 and asked[0][0] == "codex" and "сделай дело" in asked[0][2]


@pytest.mark.asyncio
async def test_orch_ops_status_skips_http_when_inactive_and_masks_secrets(tmp_path, monkeypatch):
    import sqlite3
    dbp = tmp_path / "orchestra.db"
    with sqlite3.connect(dbp) as conn:
        conn.execute("CREATE TABLE sessions (name TEXT, model TEXT, status TEXT, created_at TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('w1', 'gpt-6.2-sol', 'running', '2026-11-20')")
    monkeypatch.setattr(orch_ops, "ORCHESTRA_DB_PATH", dbp)

    async def fake_run(*argv, timeout=10.0):
        if argv[:2] == ("systemctl", "show"):
            return 0, "ActiveState=inactive\nSubState=dead\nMainPID=0\nMemoryCurrent=[not set]\nNRestarts=1", ""
        if "rev-parse" in argv:
            return 0, "abc1234", ""
        if argv[0] == "sudo" and "journalctl" in argv:
            return 0, "auth Authorization: Bearer super-secret-xyz and sk-proj-1234567890abcdef", ""
        return 0, "", ""

    monkeypatch.setattr(orch_ops, "_run_cmd", fake_run)
    st = await orch_ops.orch_status()
    assert "inactive" in st and "running=1" in st and "abc1234" in st

    blocked_restart = await orch_ops.orch_restart(force=False)
    assert "w1 (gpt-6.2-sol)" in blocked_restart and "/orch restart force" in blocked_restart

    logs_out = await orch_ops.orch_logs(10)
    assert "super-secret-xyz" not in logs_out
    assert "sk-proj-1234567890abcdef" not in logs_out
    assert "[REDACTED]" in logs_out


def test_immutable_release_deploy_and_rollback_with_loop_guard(tmp_path):
    base = tmp_path / "kesha"
    repo = tmp_path / "repo"
    (base / ".venv/bin").mkdir(parents=True)
    (base / "storage").mkdir(parents=True)
    (base / "logs").mkdir(parents=True)
    (base / ".env").write_text("OWNER_ID=720740564\n", encoding="utf-8")
    py = base / ".venv/bin/python3"
    py.symlink_to("/opt/kesha-bot/.venv/bin/python3")

    repo.mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "scripts/smoke_imports.py").write_text("print('OK')\n", encoding="utf-8")
    (repo / "mod.py").write_text("VAL = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "r1"], check=True)
    sha1 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True).strip()

    (repo / "mod.py").write_text("VAL = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "r2"], check=True)
    sha2 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True).strip()

    env = {
        **os.environ,
        "KESHA_BASE_DIR": str(base),
        "KESHA_REPO_DIR": str(repo),
        "KESHA_VENV_PYTHON": str(py),
        "KESHA_SKIP_RESTART": "1",
        "KESHA_SKIP_SYSTEMCTL": "1",
    }
    deploy_sh = str(ROOT / "scripts/deploy_release.sh")
    rollback_sh = str(ROOT / "scripts/rollback_release.sh")

    subprocess.run(["bash", deploy_sh, sha1], env=env, check=True)
    assert (base / "current").resolve() == (base / "releases" / sha1).resolve()

    subprocess.run(["bash", deploy_sh, sha2], env=env, check=True)
    assert (base / "current").resolve() == (base / "releases" / sha2).resolve()
    assert (base / "previous").resolve() == (base / "releases" / sha1).resolve()

    # Rollback shifts current -> sha1; second invocation stops cleanly via loop guard
    subprocess.run(["bash", rollback_sh], env=env, check=True)
    assert (base / "current").resolve() == (base / "releases" / sha1).resolve()
    r2 = subprocess.run(["bash", rollback_sh], env=env, capture_output=True, text=True, check=True)
    assert "loop guard triggered" in r2.stderr
