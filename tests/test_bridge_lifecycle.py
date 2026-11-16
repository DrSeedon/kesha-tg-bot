"""V-53: a persistent CLI must not keep using an expired tool credential."""

import asyncio
import json
import tomllib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import kesha_tools
import tool_bridge
from codex_session import CodexSession
from reminders import ReminderDB


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [23 * 3600 + 1, 25 * 3600])
async def test_next_turn_rotates_bridge_and_creates_reminder(tmp_path, monkeypatch, age):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(tool_bridge, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(tool_bridge, "_SESSIONS", {})
    monkeypatch.setenv(tool_bridge.TOKEN_ENV, "test-token")
    monkeypatch.setattr(kesha_tools, "_current_chat_id", kesha_tools.contextvars.ContextVar("chat"))
    db = ReminderDB(tmp_path / "reminders.db")
    bridge = tool_bridge.ToolBridge("test-token", kesha_tools.get_current_chat)

    async def create(args):
        rid = db.create(kesha_tools.get_current_chat(), args["text"],
                        datetime(2030, 1, 1, tzinfo=timezone.utc), "plain")
        return {"id": rid}

    bridge.register("create_reminder", create, allowed_args={"text"})
    session = CodexSession(str(tmp_path), chat_id=42,
                           session_file=tmp_path / "session",
                           mcp_servers={"kesha": {"type": "sdk"}})
    session.session_id = "same-thread"
    session._save_session()
    session._kesha_bridge_config()
    old_handle = session._bridge_session
    other = tool_bridge.issue_session(99, ttl=72 * 3600, runtime="codex")

    class Process:
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.returncode = 0

    old_process = Process()
    session._proc = old_process
    session._connected = True
    clock.now += age
    launched = {}
    requests = []

    async def spawn(*args, **kwargs):
        # The launch recipe, rather than the bot's field, is what MCP receives.
        config = session._mcp_config_path
        launched.update(tomllib.loads(config.read_text())["mcp_servers"]["kesha"]["env"])
        return Process()

    async def reader():
        await asyncio.Event().wait()

    async def notify(*args):
        pass

    async def request(method, params, **kwargs):
        requests.append(method)
        if method == "thread/resume":
            assert params["threadId"] == "same-thread"
            return {"thread": {"id": "same-thread"}}
        if method == "turn/start":
            class Request:
                headers = {"X-Kesha-Bridge-Token": "test-token",
                           "X-Kesha-Bridge-Session": launched["KESHA_BRIDGE_SESSION"]}

                async def json(self):
                    return {"tool": "create_reminder", "args": {"text": "test reminder"}}

            response = await bridge.handle(Request())
            assert response.status == 200
            rid = json.loads(response.body)["result"]["id"]
            assert db.get(rid)["chat_id"] == 42
            session._notifications.put_nowait({"method": "turn/completed", "params": {
                "threadId": "same-thread", "turn": {"id": "new-turn", "status": "completed"}}})
            return {"turn": {"id": "new-turn"}}
        return {}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(session, "_read_stdout", reader)
    monkeypatch.setattr(session, "_drain_stderr", reader)
    monkeypatch.setattr(session, "_request", request)
    monkeypatch.setattr(session, "_notify", notify)
    try:
        chunks = [chunk async for chunk in session.send_message("create test reminder")]
        assert not any(c["type"] == "error" for c in chunks)
        assert requests == ["initialize", "thread/resume", "turn/start"]
        assert old_process.terminated
        assert launched["KESHA_BRIDGE_SESSION"] != old_handle
        assert tool_bridge.session_chat(old_handle) is None
        assert tool_bridge.session_chat(other) == 99
        assert len(db.list_for(42)) == 1
        assert not db.list_for(99)
        assert session._session_file.read_text() == "same-thread"
    finally:
        await session.safe_disconnect()
        db.conn.close()
    assert tool_bridge.session_chat(launched["KESHA_BRIDGE_SESSION"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_healthy_process_and_active_turn_are_preserved(tmp_path, active):
    session = CodexSession(str(tmp_path), chat_id=42)
    session._bridge_session = tool_bridge.issue_session(
        42, ttl=-1 if active else 86400, runtime="codex")
    session._proc = SimpleNamespace(returncode=None)
    session._connected = True
    session._active_turn_id = "busy" if active else None
    try:
        await session._connect()
        assert session.is_alive
        assert session._active_turn_id == ("busy" if active else None)
    finally:
        session._revoke_bridge_session()


@pytest.mark.asyncio
async def test_reconnect_failure_never_starts_turn_with_stale_handle(tmp_path, monkeypatch):
    monkeypatch.setenv(tool_bridge.TOKEN_ENV, "test-token")
    session = CodexSession(str(tmp_path), chat_id=42,
                           session_file=tmp_path / "session",
                           mcp_servers={"kesha": {"type": "sdk"}})
    session.session_id = "same-thread"
    session._bridge_session = tool_bridge.issue_session(42, ttl=-1, runtime="codex")
    old = session._bridge_session
    session._proc = SimpleNamespace(returncode=0)
    session._connected = True
    # is_alive needs a running process; teardown can finish without OS signals.
    async def wait():
        session._proc = None

    session._proc = SimpleNamespace(returncode=None, terminate=lambda: None, wait=wait)

    async def fail(*args, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    chunks = [c async for c in session.send_message("create reminder")]
    assert chunks == [{"type": "error", "content": "spawn failed"}]
    assert not session.is_alive
    assert session._bridge_session is None
    assert tool_bridge.session_chat(old) is None
    assert session.session_id == "same-thread"


def test_lease_check_never_extends_or_changes_chat(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(tool_bridge, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(tool_bridge, "_SESSIONS", {})
    handle = tool_bridge.issue_session(42, ttl=100, runtime="codex")
    assert tool_bridge.session_valid_for(handle, 42, "codex", min_ttl=99)
    assert not tool_bridge.session_valid_for(handle, 99, "codex")
    assert not tool_bridge.session_valid_for(handle, 42, "claude")
    clock.now = 100
    assert not tool_bridge.session_valid_for(handle, 42, "codex")
    assert handle not in tool_bridge._SESSIONS
