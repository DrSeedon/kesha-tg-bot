"""CodexSession — contract, isolation and event mapping.

The event fixtures are taken from a real app-server run recorded in
docs/tasks/16/spikes/turn_probe_events.jsonl, not invented.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_session import (  # noqa: E402
    DEFAULT_CONTEXT_WINDOW,
    CodexProtocolError,
    CodexSession,
    _estimate_tokens,
    _format_reset,
    _toml_str,
)
from runtime_protocol import ChatRuntime, RuntimeCapabilities  # noqa: E402
from runtime_registry import (  # noqa: E402
    RuntimeBuildContext,
    build_runtime,
    get_runtime,
    list_runtimes,
)

SPIKE_EVENTS = (
    Path(__file__).resolve().parent.parent
    / "docs" / "tasks" / "16" / "spikes" / "turn_probe_events.jsonl"
)


def make_session(tmp_path, **kwargs) -> CodexSession:
    kwargs.setdefault("cwd", str(tmp_path))
    kwargs.setdefault("session_file", tmp_path / "sessions" / "42")
    return CodexSession(**kwargs)


# ---------- contract ----------


def test_satisfies_chat_runtime_protocol(tmp_path):
    assert isinstance(make_session(tmp_path), ChatRuntime)


def test_registered_in_registry_without_becoming_default(tmp_path):
    assert "codex" in list_runtimes()
    assert get_runtime("codex").capabilities is CodexSession.CAPABILITIES
    # Claude stays the default runtime; T4 must not flip it.
    import config

    assert config.RUNTIME == "claude"


def test_capabilities_are_honest():
    caps = CodexSession.CAPABILITIES
    assert isinstance(caps, RuntimeCapabilities)
    # Subscription auth reports neither dollars nor a live context percentage.
    assert caps.cost_reporting is False
    assert caps.context_percentage is False
    # #14 removed mid-turn injection; claiming it would be a lie.
    assert caps.mid_turn_inject is False
    assert caps.native_compact is True
    assert caps.resume_across_restart is True


def test_build_runtime_accepts_codex(tmp_path):
    backend = build_runtime(
        "codex",
        RuntimeBuildContext(
            chat_id=42,
            cwd=str(tmp_path),
            model="gpt-5.6-sol",
            system_prompt="be terse",
            mcp_servers={},
            session_file=tmp_path / "sessions" / "42",
        ),
    )
    assert isinstance(backend, CodexSession)
    assert backend.model == "gpt-5.6-sol"


# ---------- MCP isolation (the T3 bridge must not be bypassable) ----------


def test_only_kesha_bridge_is_configured(tmp_path):
    """A foreign MCP server must never be handed to Kesha's Codex thread.

    Measured in docs/tasks/16/spikes/mcp_isolation_probe.py: a bare app-server
    starts the user's global serena/kwin/orchestra servers. If someone later
    drops the private CODEX_HOME or the `apps` disable, this goes red.
    """
    session = make_session(
        tmp_path,
        mcp_servers={"kesha": {"command": "/usr/bin/kesha-bridge", "args": ["--stdio"]}},
    )
    args = session._mcp_config_args()

    assert "--disable" in args and "apps" in args, "built-in codex_apps must be disabled"

    configured = {
        part.split("mcp_servers.")[1].split(".")[0]
        for part in args
        if part.startswith("mcp_servers.")
    }
    assert configured == {"kesha"}, f"unexpected MCP servers configured: {configured}"


def test_private_codex_home_is_used_and_isolated(tmp_path):
    session = make_session(tmp_path, mcp_servers={})
    home = Path(session._ensure_codex_home())

    assert home.exists()
    # The whole point: NOT the user's ~/.codex, which carries the global servers.
    assert home != Path.home() / ".codex"
    config = (home / "config.toml").read_text()
    assert "mcp_servers" not in config, "private config must not define servers"
    assert oct(home.stat().st_mode)[-3:] == "700"


def test_codex_home_is_overridable(tmp_path, monkeypatch):
    target = tmp_path / "custom-home"
    monkeypatch.setenv("KESHA_CODEX_HOME", str(target))
    session = make_session(tmp_path)
    assert Path(session._ensure_codex_home()) == target


def test_apps_feature_is_disabled_not_just_config(tmp_path):
    """`codex_apps` is a built-in behind the `apps` feature flag.

    A private CODEX_HOME drops the user's config servers but NOT this one
    (measured: CODEX_HOME alone still started codex_apps). Losing this flag in
    a refactor would silently re-open a server inside Kesha's thread.
    """
    args = make_session(tmp_path, mcp_servers={})._mcp_config_args()
    assert args[:2] == ["--disable", "apps"]


def test_legacy_landlock_avoids_vps_bubblewrap_network_namespace(tmp_path):
    """The VPS cannot create bwrap's loopback interface (RTM_NEWADDR EPERM)."""
    args = make_session(tmp_path, mcp_servers={})._mcp_config_args()
    assert ["--enable", "use_legacy_landlock"] == args[2:4]


class _ServerRequestStdout:
    def __init__(self, messages):
        self._lines = [json.dumps(message).encode() + b"\n" for message in messages]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _ServerResponseStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(json.loads(data))

    async def drain(self):
        return None


def _drive_server_request(session, params, *, method="mcpServer/elicitation/request"):
    stdin = _ServerResponseStdin()
    session._proc = type("Proc", (), {
        "stdout": _ServerRequestStdout([{
            "jsonrpc": "2.0", "id": 17, "method": method, "params": params,
        }]),
        "stdin": stdin,
    })()
    session._connected = True
    asyncio.run(session._read_stdout())
    return stdin.writes


def test_configured_mcp_empty_permission_is_accepted(tmp_path):
    session = make_session(
        tmp_path,
        mcp_servers={"mailru": {"command": "/usr/bin/mailru-mcp"}},
    )
    writes = _drive_server_request(session, {
        "serverName": "mailru",
        "mode": "form",
        "message": 'Allow the mailru MCP server to run tool "mail_count_all"?',
        "requestedSchema": {"type": "object", "properties": {}},
    })
    assert writes == [{
        "jsonrpc": "2.0", "id": 17,
        "result": {"action": "accept", "content": {}},
    }]


@pytest.mark.parametrize("params", [
    {
        "serverName": "foreign",
        "mode": "form",
        "message": 'Allow the foreign MCP server to run tool "steal"?',
        "requestedSchema": {"type": "object", "properties": {}},
    },
    {
        "serverName": "mailru",
        "mode": "form",
        "message": "Send a password",
        "requestedSchema": {
            "type": "object", "properties": {"password": {"type": "string"}},
        },
    },
    {
        "serverName": "mailru",
        "mode": "url",
        "message": "Open this URL",
        "url": "https://example.invalid/authorize",
    },
])
def test_non_permission_mcp_elicitation_is_declined(tmp_path, params):
    session = make_session(
        tmp_path,
        mcp_servers={"mailru": {"command": "/usr/bin/mailru-mcp"}},
    )
    writes = _drive_server_request(session, params)
    assert writes == [{
        "jsonrpc": "2.0", "id": 17,
        "result": {"action": "decline"},
    }]


def test_unknown_server_request_gets_error_instead_of_hanging(tmp_path):
    session = make_session(tmp_path)
    writes = _drive_server_request(
        session,
        {"anything": True},
        method="item/tool/call",
    )
    assert writes[0]["id"] == 17
    assert writes[0]["error"]["code"] == -32601


@pytest.mark.skipif(
    not os.getenv("KESHA_CODEX_LIVE"),
    reason="live app-server test; set KESHA_CODEX_LIVE=1 to run",
)
def test_live_thread_declares_only_the_bridge(tmp_path):
    """FACT check, not flag check: what actually starts in Kesha's thread.

    Goes red if the user adds a global MCP server, or if Codex changes how
    CODEX_HOME / the apps flag work. This is the test that would have caught
    `-c mcp_servers={}` doing nothing.
    """

    session = make_session(tmp_path, mcp_servers={})
    started: set[str] = set()

    async def run():
        await session._connect()
        # Servers announce themselves shortly after the thread opens.
        await asyncio.sleep(6)
        while not session._notifications.empty():
            msg = session._notifications.get_nowait()
            if msg.get("method") == "mcpServer/startupStatus/updated":
                started.add((msg.get("params") or {}).get("name"))
        await session.safe_disconnect()

    asyncio.run(run())
    assert started == set(), f"foreign MCP servers leaked into Kesha's thread: {started}"


def test_mcp_args_are_toml_escaped(tmp_path):
    session = make_session(
        tmp_path,
        mcp_servers={"kesha": {"command": '/opt/we"ird/path', "args": ['a"b']}},
    )
    args = session._mcp_config_args()
    rendered = " ".join(args)
    assert '\\"' in rendered, "quotes must be escaped so the override stays parseable"


# ---------- auth linking (a re-login must not silently deauthorize Kesha) ----------


def _fake_user_home(tmp_path, monkeypatch, token="ORIGINAL"):
    user_home = tmp_path / "user-codex"
    user_home.mkdir(parents=True, exist_ok=True)
    (user_home / "auth.json").write_text(json.dumps({"token": token}))
    monkeypatch.setenv("KESHA_CODEX_USER_HOME", str(user_home))
    return user_home


def test_auth_is_linked_to_the_user_credentials(tmp_path, monkeypatch):
    user_home = _fake_user_home(tmp_path, monkeypatch)
    session = make_session(tmp_path)
    home = Path(session._ensure_codex_home())
    link = home / "auth.json"
    assert link.is_symlink()
    assert link.readlink() == user_home / "auth.json"
    assert json.loads(link.read_text())["token"] == "ORIGINAL"


@pytest.mark.parametrize("strategy", ["rewrite", "atomic_replace", "unlink_recreate"])
def test_auth_link_survives_relogin(tmp_path, monkeypatch, strategy):
    """`codex login` may rewrite, replace or recreate auth.json.

    A symlink resolves by path, so all three keep working. Verified for each
    strategy rather than assumed — a broken link means Kesha loses its
    subscription with no visible reason.
    """
    user_home = _fake_user_home(tmp_path, monkeypatch)
    target = user_home / "auth.json"
    link = Path(make_session(tmp_path)._ensure_codex_home()) / "auth.json"

    if strategy == "rewrite":
        target.write_text(json.dumps({"token": "NEW"}))
    elif strategy == "atomic_replace":
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": "NEW"}))
        os.replace(tmp, target)
    else:
        target.unlink()
        target.write_text(json.dumps({"token": "NEW"}))

    assert json.loads(link.read_text())["token"] == "NEW"


def test_dangling_auth_link_is_repaired_on_reconnect(tmp_path, monkeypatch):
    """After a logout the link dangles; a later login must be picked up."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    session = make_session(tmp_path)
    home = Path(session._ensure_codex_home())
    link = home / "auth.json"

    (user_home / "auth.json").unlink()          # logout
    assert link.is_symlink() and not link.exists()

    session._ensure_codex_home()                # reconnect while logged out
    assert not link.exists()

    (user_home / "auth.json").write_text(json.dumps({"token": "AFTER_LOGIN"}))
    session._ensure_codex_home()                # reconnect after login
    assert json.loads(link.read_text())["token"] == "AFTER_LOGIN"


def test_missing_credentials_are_reported_not_swallowed(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("KESHA_CODEX_USER_HOME", str(tmp_path / "nowhere"))
    with caplog.at_level("WARNING"):
        make_session(tmp_path)._ensure_codex_home()
    assert any("codex login" in r.message for r in caplog.records)


# ---------- event mapping (fixtures from the live run) ----------


def load_spike_events():
    if not SPIKE_EVENTS.exists():
        pytest.skip("spike events artifact missing")
    return [json.loads(line) for line in SPIKE_EVENTS.read_text().splitlines() if line.strip()]


async def drive(session: CodexSession, events: list[dict]) -> list[dict]:
    """Feed recorded notifications through the turn consumer."""
    for event in events:
        session._notifications.put_nowait(event)
    chunks = []
    async for chunk in session._consume_turn():
        chunks.append(chunk)
    return chunks


def test_recorded_turn_streams_deltas_then_turn_done(tmp_path):
    events = [
        e for e in load_spike_events()
        if e.get("method") in (
            "item/agentMessage/delta",
            "thread/tokenUsage/updated",
            "account/rateLimits/updated",
            "turn/completed",
        )
    ]
    session = make_session(tmp_path)
    chunks = asyncio.run(drive(session, events))

    deltas = [c["content"] for c in chunks if c["type"] == "text_delta"]
    assert "".join(deltas) == "Работаю — чудеса техники всё-таки случаются. 😏"
    assert chunks[-1]["type"] == "turn_done"
    assert not [c for c in chunks if c["type"] == "error"]


def test_recorded_turn_absorbs_real_context_numbers(tmp_path):
    session = make_session(tmp_path)
    asyncio.run(drive(session, load_spike_events()))
    # From the live run: last.inputTokens=18676, modelContextWindow=258400.
    assert session._context_tokens == 18676
    assert session._context_window == 258400


def test_failure_is_reported_once_not_twice(tmp_path):
    """Codex emits `error` AND turn/completed{failed}. The user gets one message."""
    session = make_session(tmp_path)
    error = {"message": "boom", "codexErrorInfo": "other"}
    chunks = asyncio.run(drive(session, [
        {"method": "error", "params": {"error": error}},
        {"method": "turn/completed", "params": {"turn": {"status": "failed", "error": error}}},
    ]))
    errors = [c for c in chunks if c["type"] == "error"]
    assert len(errors) == 1
    assert "boom" in errors[0]["content"]
    assert not [c for c in chunks if c["type"] == "turn_done"]


def test_usage_limit_is_terminal_and_carries_reset_date(tmp_path):
    session = make_session(tmp_path)
    session.rate_limit = {"primary": {"resetsAt": 1786168425}}
    chunks = asyncio.run(drive(session, [
        {"method": "turn/completed", "params": {"turn": {
            "status": "failed",
            "error": {"message": "usage limit", "codexErrorInfo": "usageLimitExceeded"},
        }}},
    ]))
    assert chunks[-1]["kind"] == "usage_limit"
    assert session.usage_limit_active is True
    # A real date, so the user is not told to simply "try later".
    assert "сброс" in chunks[-1]["content"]


def test_context_limit_is_classified_for_compaction(tmp_path):
    session = make_session(tmp_path)
    chunks = asyncio.run(drive(session, [
        {"method": "turn/completed", "params": {"turn": {
            "status": "failed",
            "error": {"message": "too long", "codexErrorInfo": "contextWindowExceeded"},
        }}},
    ]))
    assert chunks[-1]["kind"] == "context_limit"


def _delta(turn, text):
    return {"method": "item/agentMessage/delta", "params": {"turnId": turn, "delta": text}}


def _completed(turn):
    return {"method": "turn/completed",
            "params": {"turnId": turn, "turn": {"id": turn, "status": "completed"}}}


def fake_transport(session, turns: dict[str, list[dict]]):
    """Make send_message runnable without a real app-server.

    The test must drive the PUBLIC generator, not the internals: the cleanup
    lives in send_message's `finally`, so calling `_discard_turn_events` by
    hand would test the helper while leaving the wiring unverified. (That was
    the original defect in this test — removing the `finally` call kept it
    green.)
    """
    sequence = list(turns)

    async def connect():
        session._connected = True

    async def request(method, params, **kwargs):
        if method != "turn/start":
            return {}
        turn_id = sequence.pop(0)
        for event in turns[turn_id]:
            session._notifications.put_nowait(event)
        return {"turn": {"id": turn_id}}

    session._connect = connect
    session._request = request
    session.session_id = "thread-1"
    return session


def test_abandoned_turn_does_not_bleed_into_the_next_one(tmp_path):
    """User /stop must not make the next answer contain the previous one's tail.

    Drives the real send_message generator and abandons it mid-stream, exactly
    as response_stream does on /stop. Mutation-checked: deleting the cleanup in
    send_message's `finally` makes this fail.
    """
    session = fake_transport(make_session(tmp_path), {
        "A": [_delta("A", "ПЕРВЫЙ-"), _delta("A", "хвост1"),
              _delta("A", "хвост2"), _completed("A")],
        "B": [_delta("B", "ВТОРОЙ"), _completed("B")],
    })

    async def scenario():
        async for _ in session.send_message("вопрос 1"):
            break                       # user pressed /stop
        return [c async for c in session.send_message("вопрос 2")]

    chunks = asyncio.run(scenario())
    text = "".join(c["content"] for c in chunks if c["type"] == "text_delta")
    assert "хвост" not in text, f"previous turn leaked into this answer: {text!r}"
    assert text == "ВТОРОЙ"


def test_stopped_turn_leaves_no_events_behind(tmp_path):
    """After abandoning a turn, its remainder must not sit in the queue.

    Checks the observable consequence of the `finally`, so removing that call
    goes red here too.
    """
    session = fake_transport(make_session(tmp_path), {
        "A": [_delta("A", "первый"), _delta("A", "хвост1"),
              _delta("A", "хвост2"), _completed("A")],
    })

    async def scenario():
        async for _ in session.send_message("вопрос"):
            break

    asyncio.run(scenario())
    assert session._notifications.qsize() == 0, "stopped turn left events queued"


def test_stale_turn_completed_does_not_truncate_the_live_turn(tmp_path):
    """A leftover terminator from an old turn must not end the current one."""
    session = make_session(tmp_path)
    for event in (_completed("OLD"), _delta("NEW", "ЖИВОЙ"), _completed("NEW")):
        session._notifications.put_nowait(event)

    chunks = asyncio.run(_collect(session._consume_turn("NEW")))
    text = "".join(c["content"] for c in chunks if c["type"] == "text_delta")
    assert text == "ЖИВОЙ"


def test_discard_keeps_process_level_events(tmp_path):
    """Cleanup is turn-scoped: a process death must survive it."""
    session = make_session(tmp_path)
    session._notifications.put_nowait(_delta("A", "x"))
    session._notifications.put_nowait({"method": "_process/exited", "params": {}})
    session._discard_turn_events("A")

    remaining = [session._notifications.get_nowait()
                 for _ in range(session._notifications.qsize())]
    assert [m["method"] for m in remaining] == ["_process/exited"]


async def _collect(agen):
    return [chunk async for chunk in agen]


def test_compact_waits_past_the_interrupt_for_the_real_compaction(tmp_path):
    """compact/start interrupts the running turn BEFORE compacting.

    Measured order (docs/tasks/16/spikes/compact_events.txt): an interrupted
    turn/completed arrives first, then a second turn carrying the
    contextCompaction item. Returning on the first turn/completed would report
    success before compaction had begun — which is exactly what it did, and
    why a live thread's context never shrank.
    """
    session = make_session(tmp_path)
    session.session_id = "thread-1"
    session._connected = True
    session._proc = type("P", (), {"returncode": None})()  # is_alive without a real process
    order: list[str] = []

    async def fake_request(method, params, **kwargs):
        order.append(method)
        for event in (
            # the interrupt of the in-flight turn
            {"method": "turn/completed", "params": {
                "turnId": "T1", "turn": {"id": "T1", "status": "interrupted"}}},
            # the compaction turn
            {"method": "turn/started", "params": {"turnId": "T2"}},
            {"method": "item/started", "params": {
                "turnId": "T2", "item": {"type": "contextCompaction", "id": "c1"}}},
            {"method": "item/completed", "params": {
                "turnId": "T2", "item": {"type": "contextCompaction", "id": "c1"}}},
        ):
            session._notifications.put_nowait(event)
        return {}

    session._request = fake_request
    result = asyncio.run(session.compact_context())

    assert order == ["thread/compact/start"]
    assert result["max_tokens"] == DEFAULT_CONTEXT_WINDOW
    # The queue must be drained through the compaction item, not abandoned at
    # the interrupt — anything left would bleed into the next turn.
    assert session._notifications.qsize() == 0


def test_compact_does_not_claim_a_post_compaction_measurement(tmp_path):
    """Usage during compaction still reports pre-compaction totals.

    Reporting them as the new context size would be the same lie the dashboard
    told in Orchestra (aggregate usage rendered as current context).
    """
    session = make_session(tmp_path)
    session.session_id = "t"
    session._connected = True
    session._proc = type("P", (), {"returncode": None})()

    async def fake_request(method, params, **kwargs):
        session._notifications.put_nowait({"method": "item/completed", "params": {
            "item": {"type": "contextCompaction", "id": "c1"}}})
        return {}

    session._request = fake_request
    result = asyncio.run(session.compact_context())
    assert result["measured_after"] is False


def test_process_exit_ends_the_stream(tmp_path):
    session = make_session(tmp_path)
    chunks = asyncio.run(drive(session, [{"method": "_process/exited", "params": {}}]))
    assert chunks[-1]["type"] == "error"
    assert session._active_turn_id is None


def test_tool_calls_map_to_status_bubble(tmp_path):
    session = make_session(tmp_path)
    chunks = asyncio.run(drive(session, [
        {"method": "item/started", "params": {"item": {
            "type": "mcpToolCall", "server": "kesha", "tool": "send_photo",
            "arguments": {"path": "a.png"},
        }}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]))
    tools = [c for c in chunks if c["type"] == "tool"]
    assert tools == [{"type": "tool", "name": "mcp__kesha__send_photo",
                      "input": {"path": "a.png"}}]


def test_reasoning_items_do_not_leak_into_the_answer(tmp_path):
    """Kesha is a chat bot: internal reasoning must not be sent to the user."""
    session = make_session(tmp_path)
    chunks = asyncio.run(drive(session, [
        {"method": "item/started", "params": {"item": {"type": "reasoning", "id": "r1"}}},
        {"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1", "content": ["secret chain of thought"],
        }}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]))
    assert not any("secret" in json.dumps(c, ensure_ascii=False) for c in chunks)


# ---------- context reserve ----------


def test_reserve_is_open_but_honest_before_any_turn(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    monkeypatch.setattr(session, "_connect", _noop_async)
    result = asyncio.run(session.check_context_reserve("hi"))
    assert result["ok"] is True
    # Must not claim a verified headroom it never measured.
    assert result["remaining"] is None


def test_reserve_blocks_when_context_is_nearly_full(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    monkeypatch.setattr(session, "_connect", _noop_async)
    session._context_window = DEFAULT_CONTEXT_WINDOW
    session._context_tokens = DEFAULT_CONTEXT_WINDOW - 100
    result = asyncio.run(session.check_context_reserve("hello"))
    assert result["ok"] is False
    assert result["reason"] == "reserve"


def test_reserve_admits_a_roomy_thread(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    monkeypatch.setattr(session, "_connect", _noop_async)
    session._context_tokens = 18_676
    result = asyncio.run(session.check_context_reserve("hello"))
    assert result["ok"] is True
    assert result["remaining"] == DEFAULT_CONTEXT_WINDOW - 18_676


def test_reserve_reports_session_unavailable(tmp_path, monkeypatch):
    session = make_session(tmp_path)

    async def boom():
        raise RuntimeError("session_unavailable: gone")

    monkeypatch.setattr(session, "_connect", boom)
    result = asyncio.run(session.check_context_reserve("hi"))
    assert result == {"ok": False, "reason": "session_unavailable", "required": result["required"]}


def test_manual_compact_uses_the_lower_floor(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    monkeypatch.setattr(session, "_connect", _noop_async)
    normal = asyncio.run(session.check_context_reserve("x" * 1000))["required"]
    manual = asyncio.run(session.check_context_reserve(manual=True))["required"]
    assert manual < normal


async def _noop_async(*args, **kwargs):
    return None


# ---------- session persistence ----------


def test_thread_id_survives_restart(tmp_path):
    session_file = tmp_path / "sessions" / "42"
    first = make_session(tmp_path, session_file=session_file)
    first.session_id = "019fbcce-fff0-7353-9816-402d1ce28e01"
    first._save_session()

    second = make_session(tmp_path, session_file=session_file)
    assert second.session_id == "019fbcce-fff0-7353-9816-402d1ce28e01"


def test_reset_clears_the_persisted_thread(tmp_path):
    session_file = tmp_path / "sessions" / "42"
    session = make_session(tmp_path, session_file=session_file)
    session.session_id = "thread-1"
    session._save_session()
    session._context_tokens = 5000

    asyncio.run(session.reset_async())

    assert session.session_id is None
    assert session._context_tokens is None
    assert make_session(tmp_path, session_file=session_file).session_id is None


def test_reconnect_keeps_the_thread_id(tmp_path):
    session = make_session(tmp_path)
    session.session_id = "thread-1"
    session.reconnect()
    assert session.session_id == "thread-1"


def test_safe_disconnect_is_quiet_when_never_started(tmp_path):
    session = make_session(tmp_path)
    asyncio.run(session.safe_disconnect())
    asyncio.run(session.interrupt())  # no active turn — must not raise


def test_legacy_underscore_disconnect_alias_exists(tmp_path):
    # chat_state.py:959 still calls the private name.
    assert make_session(tmp_path)._safe_disconnect is not None


# ---------- helpers ----------


def test_toml_escaping():
    assert _toml_str('a"b') == '"a\\"b"'
    assert _toml_str("a\\b") == '"a\\\\b"'


def test_token_estimate_never_underestimates_cyrillic():
    # Cyrillic is ~2 chars/token; the estimate must not fall below that.
    assert _estimate_tokens("привет" * 100) >= 300
    assert _estimate_tokens("") == 0


def test_reset_formatting_is_defensive():
    assert _format_reset(None) == ""
    assert _format_reset(0) == ""
    assert "." in _format_reset(1786168425)


def test_protocol_error_keeps_the_reason_visible():
    exc = CodexProtocolError("turn/start", {"message": "nope"})
    assert "nope" in str(exc)
    assert exc.method == "turn/start"
