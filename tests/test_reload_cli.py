"""#36 — /reload: respawn the CLI on a new MCP set without losing the chat.

The failure this guards against is silent in both directions: a reload that
does not actually re-read the file looks identical to a working one, and a
reload that starts a fresh conversation looks like success until the bot
forgets who it was talking to.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_state import ChatPhase, ChatState, PendingEntry  # noqa: E402
from claude_session import ClaudeSession  # noqa: E402
from codex_session import CodexSession  # noqa: E402
from config import load_mcp_servers  # noqa: E402


# ---------- the file is re-read, not remembered ----------


def _write_mcp(path: Path, *names: str) -> None:
    path.write_text(json.dumps({
        "mcpServers": {n: {"command": "node", "args": [f"/opt/{n}.js"]} for n in names}
    }))


def test_reload_sees_a_server_added_after_startup(tmp_path):
    """The whole point of the command: the file on disk wins over memory."""
    config_path = tmp_path / ".mcp.json"
    _write_mcp(config_path, "ozon")

    before = load_mcp_servers(str(tmp_path))
    _write_mcp(config_path, "ozon", "yougile")
    after = load_mcp_servers(str(tmp_path))

    assert "yougile" not in before
    assert "yougile" in after
    assert after["yougile"]["args"] == ["/opt/yougile.js"]


def test_unreadable_mcp_json_is_reported(tmp_path, caplog):
    """A typo in .mcp.json is the usual reason to run /reload — say so."""
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {,,,}')

    with caplog.at_level("WARNING", logger="kesha"):
        servers = load_mcp_servers(str(tmp_path))

    assert "yougile" not in servers
    assert any(".mcp.json is unreadable" in r.message for r in caplog.records), (
        "a broken config was swallowed silently"
    )


# ---------- ClaudeSession: the respawn carries the new set and the old thread ----------


class FakeSDKClient:
    """Records the options the SDK would have spawned the CLI with."""

    spawned: list = []

    def __init__(self, options):
        self.options = options
        FakeSDKClient.spawned.append(options)

    async def connect(self):
        return None

    async def disconnect(self):
        return None


def make_claude(tmp_path, monkeypatch, session_id="sid-old"):
    monkeypatch.setattr("claude_session.ClaudeSDKClient", FakeSDKClient)
    FakeSDKClient.spawned = []
    session = ClaudeSession(
        cwd=str(tmp_path),
        mcp_servers={"kesha": {"type": "stdio", "command": "old"}},
        session_file=tmp_path / "sess",
    )
    session.session_id = session_id
    return session


def test_claude_reload_respawns_with_the_new_servers_and_resumes(tmp_path, monkeypatch):
    session = make_claude(tmp_path, monkeypatch)

    asyncio.run(session.apply_mcp_servers({
        "kesha": {"type": "stdio", "command": "old"},
        "ozon": {"type": "stdio", "command": "ssh"},
    }))

    assert len(FakeSDKClient.spawned) == 1, "the CLI was not respawned"
    options = FakeSDKClient.spawned[0]
    assert options.resume == "sid-old", "the respawned CLI did not resume the thread"
    external = json.loads(Path(options.extra_args["mcp-config"]).read_text())
    assert set(external["mcpServers"]) == {"kesha", "ozon"}, (
        "the new server did not reach the respawned CLI"
    )
    assert session.session_id == "sid-old"


def test_claude_reload_preserves_the_session_across_a_failed_connect(tmp_path, monkeypatch):
    """A resume error must not be turned into a brand-new conversation."""
    session = make_claude(tmp_path, monkeypatch)

    class Refusing(FakeSDKClient):
        async def connect(self):
            raise RuntimeError("No conversation found")

    monkeypatch.setattr("claude_session.ClaudeSDKClient", Refusing)

    with pytest.raises(RuntimeError, match="No conversation found"):
        asyncio.run(session.apply_mcp_servers({"kesha": {"type": "stdio", "command": "x"}}))

    assert session.session_id == "sid-old", "the session was invalidated by a reload"


def test_claude_reload_raises_when_the_session_changes(tmp_path, monkeypatch):
    session = make_claude(tmp_path, monkeypatch)

    async def connect_to_another_conversation(*, preserve_session=False):
        session.session_id = "sid-new"

    session._ensure_connected = connect_to_another_conversation

    with pytest.raises(RuntimeError, match="lost the conversation"):
        asyncio.run(session.apply_mcp_servers({"kesha": {"type": "stdio", "command": "x"}}))


def test_claude_reload_of_a_fresh_chat_is_allowed(tmp_path, monkeypatch):
    """No session_id yet means there is no conversation to lose."""
    session = make_claude(tmp_path, monkeypatch, session_id=None)

    asyncio.run(session.apply_mcp_servers({"kesha": {"type": "stdio", "command": "x"}}))

    assert session.session_id is None


# ---------- CodexSession: same discipline on its own process ----------


def test_codex_reload_swaps_servers_before_the_app_server_starts(tmp_path):
    session = CodexSession(
        chat_id=42,
        cwd=str(tmp_path),
        mcp_servers={"kesha": {"command": "old"}},
        session_file=tmp_path / "sess",
    )
    session.session_id = "thread-1"
    seen: list[list[str]] = []

    async def fake_connect():
        # config.toml is written from self.mcp_servers at spawn — a swap that
        # happens after this point would launch the old set.
        seen.append(sorted(session.mcp_servers))

    async def fake_teardown():
        return None

    session._connect = fake_connect
    session._teardown_process = fake_teardown

    asyncio.run(session.apply_mcp_servers({"kesha": {"command": "old"}, "ozon": {"command": "ssh"}}))

    assert seen == [["kesha", "ozon"]]
    assert session.session_id == "thread-1"


def test_codex_reload_raises_when_the_thread_changes(tmp_path):
    session = CodexSession(
        chat_id=42,
        cwd=str(tmp_path),
        mcp_servers={},
        session_file=tmp_path / "sess",
    )
    session.session_id = "thread-1"

    async def fake_connect():
        session.session_id = "thread-2"

    async def fake_teardown():
        return None

    session._connect = fake_connect
    session._teardown_process = fake_teardown

    with pytest.raises(RuntimeError, match="lost the thread"):
        asyncio.run(session.apply_mcp_servers({}))


# ---------- ChatState: the turn is never torn down ----------


class FakeReloadSession:
    def __init__(self, servers=("kesha",), session_id="sid-1", fail=None):
        self.mcp_servers = {name: {"command": name} for name in servers}
        self.session_id = session_id
        self.model = "m"
        self.applied = 0
        self._fail = fail
        self.gate: asyncio.Event | None = None

    async def apply_mcp_servers(self, servers):
        if self.gate is not None:
            await self.gate.wait()
        if self._fail:
            raise RuntimeError(self._fail)
        self.applied += 1
        self.mcp_servers = servers

    async def safe_disconnect(self):
        return None


class FakeStore:
    def begin_activity(self, chat_id, now_utc=None):
        return ""

    def finish_activity(self, chat_id, now_utc=None):
        return ""

    def get_activity(self, chat_id):
        return None


def make_chat(tmp_path, session, loader):
    return ChatState(
        chat_id=42,
        session=session,
        bot=None,
        debounce_sec=1,
        ask_fn=None,
        set_current_chat_fn=lambda cid: None,
        get_lazy_block_fn=lambda cid: ("", [], []),
        compact_session_fn=None,
        activity_store=FakeStore(),
        work_dir=str(tmp_path),
        reload_mcp_fn=loader,
    )


@pytest.mark.parametrize("phase", [
    ChatPhase.PROCESSING, ChatPhase.STOPPING, ChatPhase.COMPACTING,
])
def test_reload_refused_while_a_turn_is_active(tmp_path, phase):
    session = FakeReloadSession()
    chat = make_chat(tmp_path, session, lambda: {"kesha": {}, "ozon": {}})
    chat.phase = phase

    result = asyncio.run(chat.reload_cli())

    assert result["ok"] is False
    assert result["reason"] == "busy"
    assert session.applied == 0, "the CLI was restarted under a live turn"
    assert chat.phase is phase, "phase was disturbed by a refused reload"


def test_reload_reports_what_changed(tmp_path):
    session = FakeReloadSession(servers=("kesha", "yougile"))
    chat = make_chat(tmp_path, session, lambda: {"kesha": {}, "ozon": {}})

    result = asyncio.run(chat.reload_cli())

    assert result["ok"] is True
    assert result["servers"] == ["kesha", "ozon"]
    assert result["added"] == ["ozon"]
    assert result["removed"] == ["yougile"]
    assert result["session"] == "sid-1"
    assert chat.phase is ChatPhase.IDLE


def test_reload_failure_does_not_latch_the_chat(tmp_path):
    session = FakeReloadSession(fail="app-server did not start")
    chat = make_chat(tmp_path, session, lambda: {"kesha": {}})

    result = asyncio.run(chat.reload_cli())

    assert result["ok"] is False
    assert result["reason"] == "failed"
    assert "did not start" in result["error"]
    assert chat.phase is ChatPhase.IDLE, "a failed reload left the chat unusable"


def test_a_message_sent_during_the_reload_is_answered_afterwards(tmp_path):
    """The user's turn is never dropped — it waits for the new toolset."""
    session = FakeReloadSession()
    chat = make_chat(tmp_path, session, lambda: {"kesha": {}, "ozon": {}})
    processed: list[list[PendingEntry]] = []

    async def record_batch(batch):
        processed.append(batch)
        async with chat._lock:
            chat.phase = ChatPhase.IDLE

    chat._start_processing = record_batch

    async def scenario():
        session.gate = asyncio.Event()
        task = asyncio.create_task(chat.reload_cli())
        await asyncio.sleep(0)  # let reload_cli take the phase
        await chat.accept_entry(PendingEntry(prompt="привет", message_id=1))
        assert chat.pending == [], "a message slipped into the live reload"
        session.gate.set()
        return await task

    result = asyncio.run(scenario())

    assert result["ok"] is True
    assert [e.prompt for batch in processed for e in batch] == ["привет"]
    assert chat.deferred == []


# ---------- what the user actually sees ----------


def _msg(text="/reload", uid=1):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=uid, language_code="ru"),
        chat=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )


def test_reload_is_published_in_both_menus():
    import handlers

    assert "reload" in {c.command for c in handlers.COMMANDS_RU}
    assert "reload" in {c.command for c in handlers.COMMANDS_EN}
    for commands in (handlers.COMMANDS_RU, handlers.COMMANDS_EN):
        description = next(c.description for c in commands if c.command == "reload")
        assert "MCP" in description


@pytest.mark.asyncio
async def test_reload_reply_names_the_servers_and_the_change(monkeypatch):
    import handlers

    state = SimpleNamespace(reload_cli=AsyncMock(return_value={
        "ok": True, "servers": ["kesha", "ozon"], "added": ["ozon"],
        "removed": [], "session": "sid-1",
    }))
    monkeypatch.setattr(handlers, "_registry", SimpleNamespace(get=lambda _cid: state))
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    msg = _msg()

    await handlers.h_reload(msg)

    text = msg.answer.await_args.args[0]
    assert "2" in text and "kesha, ozon" in text
    assert "ozon" in text.split("Добавились")[1]
    assert "sid-1" in text


@pytest.mark.asyncio
async def test_reload_refusal_is_spoken_not_swallowed(monkeypatch):
    import handlers

    state = SimpleNamespace(reload_cli=AsyncMock(
        return_value={"ok": False, "reason": "busy", "phase": "processing"}
    ))
    monkeypatch.setattr(handlers, "_registry", SimpleNamespace(get=lambda _cid: state))
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    msg = _msg()

    await handlers.h_reload(msg)

    assert "/stop" in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_restart_targets_the_unit_that_exists(monkeypatch):
    """`kesha-bot` is not a unit on the VPS — /restart was a no-op (#27)."""
    import handlers

    argv: list[str] = []

    async def fake_exec(*cmd, **kwargs):
        argv.extend(cmd)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")))

    monkeypatch.setattr(handlers.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(handlers, "ALLOWED", {1})

    await handlers.h_restart(_msg("/restart"))

    assert argv[-1] == "kesha-bot-vps"
