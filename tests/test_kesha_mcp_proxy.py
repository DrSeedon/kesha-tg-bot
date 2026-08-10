import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import kesha_tools as kt
import tool_bridge
from kesha_mcp_proxy import exposed_tools


ROOT = Path(__file__).resolve().parent.parent


def test_proxy_exposes_telegram_tools_but_not_laptop_shell():
    names = {tool.name for tool in exposed_tools()}
    assert "send_file" in names
    assert "send_photo" in names
    assert "send_video" in names
    assert "run_on_laptop" not in names


@pytest.mark.asyncio
async def test_stdio_proxy_sends_file_to_bound_telegram_chat(tmp_path, monkeypatch):
    socket = tmp_path / "bridge.sock"
    sendable = tmp_path / "sendable"
    sendable.mkdir()
    payload = sendable / "lab.html"
    payload.write_text("<h1>lab</h1>")
    delivered = []

    class FakeBot:
        async def send_document(self, **kwargs):
            delivered.append(kwargs)

    monkeypatch.setenv("KESHA_SENDABLE_ROOTS", str(sendable))
    monkeypatch.setattr(tool_bridge, "SOCKET_PATH", socket)
    monkeypatch.setattr(
        kt, "_current_chat_id", kt.contextvars.ContextVar("proxy_test", default=None)
    )
    monkeypatch.setattr(kt, "_bot_ref", SimpleNamespace(bot=FakeBot(), ALLOWED={42}))
    tool_bridge._SESSIONS.clear()

    bridge = tool_bridge.ToolBridge(token="secret", resolve_chat=kt.get_current_chat)
    kt.register_bridge_tools(bridge)
    await bridge.start()
    handle = tool_bridge.issue_session(42, runtime="codex")
    env = {
        **os.environ,
        "KESHA_BRIDGE_SOCKET": str(socket),
        "KESHA_BRIDGE_TOKEN": "secret",
        "KESHA_BRIDGE_SESSION": handle,
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "kesha_mcp_proxy.py")],
        env=env,
        cwd=str(ROOT),
    )
    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert "send_file" in {tool.name for tool in tools.tools}
                result = await client.call_tool(
                    "send_file", {"path": str(payload), "caption": "lab"}
                )
                assert result.isError is False
    finally:
        await bridge.stop()

    assert len(delivered) == 1
    assert delivered[0]["chat_id"] == 42
    assert delivered[0]["caption"] == "lab"
