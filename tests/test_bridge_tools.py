"""T3b-2 — the 14 bridge-exposed tools, their derived whitelists, chat resolution."""

import json

import pytest

import kesha_tools as kt
from kesha_tools import (
    ALL_TOOLS,
    BRIDGE_EXCLUDED,
    bridge_tools,
    register_bridge_tools,
    tool_arg_names,
)
from tool_bridge import ToolBridge


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(kt, "_active_chat_id", 4242)
    b = ToolBridge(token="t", resolve_chat=kt.get_current_chat)
    register_bridge_tools(b)
    return b


async def _call(bridge: ToolBridge, tool: str, args: dict):
    class _Req:
        headers = {"X-Kesha-Bridge-Token": "t"}

        async def json(self):
            return {"tool": tool, "args": args}

    return await bridge.handle(_Req())  # type: ignore[arg-type]


def _body(resp):
    return json.loads(resp.body)


def test_dangerous_tools_are_not_exposed_yet():
    """send_file (arbitrary path) and run_on_laptop (SSH) wait for T3c."""
    names = {t.name for t in bridge_tools()}
    assert BRIDGE_EXCLUDED == {"send_file", "run_on_laptop"}
    assert not (names & BRIDGE_EXCLUDED)
    assert len(names) == 14


def test_all_tools_accounted_for():
    assert len(ALL_TOOLS) == 16
    assert len(bridge_tools()) + len(BRIDGE_EXCLUDED) == 16


@pytest.mark.parametrize("tool", bridge_tools(), ids=lambda t: t.name)
def test_allowed_args_derived_from_schema(tool):
    """The whitelist must come from the tool's own schema, never a hand list."""
    assert tool_arg_names(tool) == frozenset(tool.input_schema.keys())


def test_registered_whitelist_matches_schema(bridge):
    for tool in bridge_tools():
        assert bridge._allowed_args[tool.name] == frozenset(tool.input_schema.keys())


def test_every_tool_is_registered(bridge):
    assert set(bridge.tools) == {t.name for t in bridge_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", bridge_tools(), ids=lambda t: t.name)
async def test_extra_argument_rejected_for_every_tool(bridge, tool):
    """Negative test per tool: an unexpected key never reaches the handler."""
    resp = await _call(bridge, tool.name, {"totally_unexpected": 1})
    assert resp.status == 400
    assert "not accepted" in _body(resp)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", bridge_tools(), ids=lambda t: t.name)
async def test_chat_id_rejected_for_every_tool(bridge, tool):
    resp = await _call(bridge, tool.name, {"chat_id": 999})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_chat_resolved_server_side_not_from_args(bridge, monkeypatch):
    """The whole point: the tool acts on the server's chat, caller cannot steer it."""
    seen = {}

    async def fake_search(args):
        seen["chat"] = kt.get_current_chat()
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setitem(bridge._handlers, "search_memory", fake_search)
    resp = await _call(bridge, "search_memory", {"query": "hi"})
    assert resp.status == 200
    assert seen["chat"] == 4242


@pytest.mark.asyncio
async def test_no_active_chat_blocks_tool(monkeypatch):
    monkeypatch.setattr(kt, "_active_chat_id", None)
    monkeypatch.setattr(kt, "_current_chat_id", kt.contextvars.ContextVar("x", default=None))
    b = ToolBridge(token="t", resolve_chat=kt.get_current_chat)
    register_bridge_tools(b)
    resp = await _call(b, "get_bot_status", {})
    assert resp.status == 409


def test_active_chat_mirror_survives_foreign_context(monkeypatch):
    """A ContextVar set in one task is invisible to the bridge's handler task."""
    monkeypatch.setattr(kt, "_active_chat_id", None)
    kt.set_current_chat(777)
    assert kt._active_chat_id == 777
    # Fresh ContextVar = a different execution context, as in an aiohttp handler.
    monkeypatch.setattr(kt, "_current_chat_id", kt.contextvars.ContextVar("y", default=None))
    assert kt.get_current_chat() == 777
