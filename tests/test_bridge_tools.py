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
    monkeypatch.setattr(
        kt, "_current_chat_id", kt.contextvars.ContextVar("test_chat", default=4242)
    )
    b = ToolBridge(token="t", resolve_chat=kt.get_current_chat)
    register_bridge_tools(b)
    return b


async def _call(bridge: ToolBridge, tool: str, args: dict, session: str | None = None):
    headers = {"X-Kesha-Bridge-Token": "t"}
    if session:
        headers["X-Kesha-Bridge-Session"] = session

    class _Req:
        def __init__(self):
            self.headers = headers

        async def json(self):
            return {"tool": tool, "args": args}

    return await bridge.handle(_Req())  # type: ignore[arg-type]


def _body(resp):
    return json.loads(resp.body)


def test_dangerous_tools_are_not_exposed_yet():
    """run_on_laptop (SSH to the user's machine) waits for T3c-3.

    Path-taking tools are exposed only because file_access gates every path.
    """
    names = {t.name for t in bridge_tools()}
    assert BRIDGE_EXCLUDED == {"run_on_laptop"}
    assert not (names & BRIDGE_EXCLUDED)
    assert len(names) == 15


@pytest.mark.parametrize(
    "tool_name", ["send_file", "send_photo", "send_video", "send_audio", "send_voice"]
)
def test_path_taking_tools_are_gated(tool_name):
    """Every exposed tool with a `path` argument must go through the whitelist."""
    import inspect

    tool = next(t for t in ALL_TOOLS if t.name == tool_name)
    source = inspect.getsource(tool.handler)
    # open_sendable validates AND reads: passing a path onward would reopen the
    # file later and reintroduce the swap window closed in the T3 review.
    assert "open_sendable" in source, f"{tool_name} accepts an unchecked path"
    assert "FSInputFile" not in source, f"{tool_name} re-opens the path after validation"


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

    from tool_bridge import issue_session

    monkeypatch.setitem(bridge._handlers, "search_memory", fake_search)
    resp = await _call(
        bridge, "search_memory", {"query": "hi"}, session=issue_session(4242)
    )
    assert resp.status == 200
    assert seen["chat"] == 4242


@pytest.mark.asyncio
async def test_no_active_chat_blocks_tool(monkeypatch):
    monkeypatch.setattr(kt, "_current_chat_id", kt.contextvars.ContextVar("x", default=None))
    b = ToolBridge(token="t", resolve_chat=kt.get_current_chat)
    register_bridge_tools(b)
    resp = await _call(b, "get_bot_status", {})
    assert resp.status == 409


def test_no_process_wide_chat_fallback():
    """Regression guard: a "last chat wins" global leaked A's output into B's chat."""
    assert not hasattr(kt, "_active_chat_id"), (
        "process-wide chat fallback reintroduced — use a bridge session instead"
    )
