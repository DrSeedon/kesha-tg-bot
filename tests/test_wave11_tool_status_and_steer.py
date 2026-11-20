import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import antigravity_session as agy
import kesha_tools as kt
import limits
from runtime_registry import RuntimeBuildContext, build_runtime
from tool_status import ToolStatusTracker, _tool_icon, _tool_short_name


class _BurstFakeBot:
    def __init__(self, *, flood_on_edit: bool = False):
        self.sent = []
        self.edits = []
        self.flood_on_edit = flood_on_edit

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=101)

    async def edit_message_text(self, text, **kwargs):
        if self.flood_on_edit:
            raise RuntimeError("Telegram server says - Too Many Requests: retry after 25")
        self.edits.append((text, kwargs))


def test_tool_status_formats_acp_and_mcp_titles():
    assert _tool_icon("File /opt/cog-second-brain/.orchestra/kb/rules-access.md") == "📖"
    assert _tool_short_name("File /opt/cog-second-brain/.orchestra/kb/rules-access.md") == "rules-access.md"

    assert _tool_icon("Run mcp_mailru_mail_folders") == "📧"
    assert _tool_short_name("Run mcp_mailru_mail_folders") == "mail_folders"

    assert _tool_icon("Run mcp_gmail_send_gmail_message") == "📧"
    assert _tool_short_name("Run mcp_gmail_send_gmail_message") == "send_gmail_message"

    assert _tool_icon("mcp_orchestra_0eaef74b_send_message") == "🎼"
    assert _tool_short_name("mcp_orchestra_0eaef74b_send_message") == "send_message"

    assert _tool_icon("mcp_gpb_accounts") == "🏦"
    assert _tool_icon("mcp_ozon_search") == "🛒"
    assert _tool_icon("mcp_hub-images_generate") == "🖼"

    assert _tool_icon("Run python3 -c \"a=1\nb=2\"") == "🖥"
    short_cmd = _tool_short_name("Run python3 -c \"a=1\nb=2\"")
    assert "\n" not in short_cmd and "python3" in short_cmd


def test_tool_status_coalesces_burst_and_respects_flood_without_stalling():
    async def run():
        bot = _BurstFakeBot(flood_on_edit=True)
        tracker = ToolStatusTracker(bot, None, 42)
        t0 = time.monotonic()

        # First call sends initial status_msg
        await tracker.add_tool("File /tmp/one.md", {})
        assert tracker.status_msg is not None
        assert len(bot.sent) == 1

        # Simulate active flood wait triggered by edit
        tracker._last_edit_ts = 0.0
        await tracker.add_tool("Run mcp_mailru_mail_folders", {})
        assert tracker._flood_until > time.time()

        # Now hammer 25 rapid add_tool + complete_current + finalize calls
        for i in range(25):
            await tracker.add_tool(f"File /tmp/f{i}.md", {})
            await tracker.complete_current()
        msg_id = await tracker.finalize()
        elapsed = time.monotonic() - t0
        return elapsed, msg_id

    elapsed, msg_id = asyncio.run(run())
    assert elapsed < 0.3, f"25 flood-throttled updates took {elapsed:.3f}s"
    assert msg_id == 101


@pytest.mark.asyncio
async def test_antigravity_slow_cancel_steer_completes_step2_seamlessly(tmp_path, monkeypatch):
    path = tmp_path / "mux.sock"
    monkeypatch.setattr(agy, "MUX_PATH", path)
    step1_done = asyncio.Event()
    prompts = []

    async def stand(reader, writer):
        sid = "slow-cancel-sid"

        def send(**msg):
            writer.write((json.dumps(msg) + "\n").encode())

        async def receive():
            raw = await reader.readline()
            return json.loads(raw) if raw else None

        try:
            open_msg = await receive()
            assert open_msg["method"] == "session/open"
            send(id=open_msg["id"], result={"sessionId": sid, "audio": True})

            first = await receive()
            assert first["method"] == "session/prompt"
            prompts.append(first)
            send(method="session/submitted", params={"sessionId": sid})
            send(method="session/update", params={
                "sessionId": sid,
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "part1 "}},
            })

            cancel = await receive()
            assert cancel["method"] == "session/cancel"
            assert cancel["params"]["sessionId"] == sid
            send(id=cancel["id"], result={"submitted": True})

            # Hold prompt settle past old 5s timeout (6.2s < 30s new timeout)
            await asyncio.sleep(6.2)
            send(id=first["id"], result={"stopReason": "cancelled", "accounting": {"cost_usd": 0.01}})
            step1_done.set()

            second = await receive()
            assert second["method"] == "session/prompt"
            assert second["params"]["sessionId"] == sid
            prompts.append(second)
            send(method="session/submitted", params={"sessionId": sid})
            send(method="session/update", params={
                "sessionId": sid,
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "part2"}},
            })
            send(id=second["id"], result={"stopReason": "end_turn", "accounting": {"cost_usd": 0.02}})
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(stand, path)
    path.chmod(0o600)
    session = build_runtime(
        "antigravity",
        RuntimeBuildContext(1, str(tmp_path), "gemini-4-hydrogen", "", {}, tmp_path / "sess.antigravity"),
    )

    seen_events = []

    async def collect():
        async for ev in session.send_message("first prompt", interactive_user=True, origin="telegram"):
            seen_events.append(ev)

    try:
        turn_task = asyncio.create_task(collect())
        while len(prompts) < 1:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)

        steered = await session.steer("follow up", interactive_user=True)
        assert steered == "submitted"
        assert step1_done.is_set()
        await asyncio.wait_for(turn_task, timeout=15)
    finally:
        server.close()
        await server.wait_closed()

    done_events = [e for e in seen_events if e.get("type") == "turn_done"]
    assert len(done_events) == 2
    assert all(e.get("ok") is True for e in done_events)
    texts = "".join(e.get("content", "") for e in seen_events if e.get("type") == "text_delta")
    assert "part1" in texts and "part2" in texts


@pytest.mark.asyncio
async def test_send_to_orchestrator_sends_kesha_handoff_headers(monkeypatch):
    from aiohttp import web

    captured = {}
    secret = "synthetic-internal-tok-xyz"
    monkeypatch.setattr(limits, "orchestra_token", lambda: secret)

    async def handle_send(request):
        captured["path"] = request.path
        captured["auth"] = request.headers.get("Authorization")
        captured["handoff"] = request.headers.get("X-Orchestra-Kesha-Handoff")
        captured["body"] = await request.json()
        return web.json_response({"ok": True, "target": "seedon-orchestrator"})

    app = web.Application()
    app.router.add_post("/api/sessions/{name}/send", handle_send)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    try:
        monkeypatch.setattr(limits, "_orchestra_url", lambda p: f"http://127.0.0.1:{port}{p}")
        res = await kt.send_to_orchestrator.handler({
            "message": "[от Кеши] Тестовый дайджест",
            "orchestrator": "seedon-orchestrator",
            "scope": "/home/kesha/projects/seedon",
        })
    finally:
        await runner.cleanup()

    assert not res.get("is_error")
    assert secret not in json.dumps(res)
    assert captured["path"] == "/api/sessions/seedon-orchestrator/send"
    assert captured["auth"] == f"Bearer {secret}"
    assert captured["handoff"] == "1"
    assert captured["body"] == {
        "message": "[от Кеши] Тестовый дайджест",
        "scope": "/home/kesha/projects/seedon",
        "channel": "kesha",
    }
