"""T3c-1 — chat addressing must come from the bridge session, not a process global.

ALLOWED_USERS holds two users in production. With a single process-wide mirror,
user A's tool call resolves to whichever chat spoke last — so A's file or reminder
lands in B's chat. These tests pin the failure and the fix.
"""

import json

import pytest

import kesha_tools as kt
from tool_bridge import ToolBridge, issue_session


@pytest.fixture(autouse=True)
def clean_chat_context(monkeypatch):
    """Each test starts with no ambient chat, so leaks cannot hide behind state."""
    monkeypatch.setattr(
        kt, "_current_chat_id", kt.contextvars.ContextVar("test_chat", default=None)
    )


@pytest.fixture
def bridge():
    delivered = []

    async def whoami(args):
        delivered.append(kt.get_current_chat())
        return {"chat": kt.get_current_chat()}

    b = ToolBridge(token="t", resolve_chat=kt.get_current_chat)
    b.register("whoami", whoami, allowed_args=set())
    b.delivered = delivered  # type: ignore[attr-defined]
    return b


async def _call(bridge: ToolBridge, tool: str, args: dict, session: str | None = None):
    headers = {"X-Kesha-Bridge-Token": "t"}
    if session is not None:
        headers["X-Kesha-Bridge-Session"] = session

    class _Req:
        def __init__(self):
            self.headers = headers

        async def json(self):
            return {"tool": tool, "args": args}

    return await bridge.handle(_Req())  # type: ignore[arg-type]


def _body(resp):
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_interleaved_chats_do_not_cross_talk(bridge):
    """RED before the fix: A's call resolved to B because B spoke last."""
    chat_a, chat_b = 1001, 2002
    session_a = issue_session(chat_a)
    session_b = issue_session(chat_b)

    # Both chats are active; B spoke most recently.
    kt.set_current_chat(chat_a)
    kt.set_current_chat(chat_b)

    # A's tool call must still be delivered to A.
    resp = await _call(bridge, "whoami", {}, session=session_a)
    assert resp.status == 200
    assert _body(resp)["result"]["chat"] == chat_a, "A's call leaked into another chat"

    resp = await _call(bridge, "whoami", {}, session=session_b)
    assert _body(resp)["result"]["chat"] == chat_b


@pytest.mark.asyncio
async def test_session_token_is_not_a_caller_argument(bridge):
    """The session is issued by the server; the caller cannot forge a chat via args."""
    session = issue_session(1001)
    resp = await _call(bridge, "whoami", {"chat_id": 2002}, session=session)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_unknown_session_is_rejected(bridge):
    resp = await _call(bridge, "whoami", {}, session="not-a-real-session")
    assert resp.status == 401
    assert bridge.delivered == []


@pytest.mark.asyncio
async def test_missing_session_uses_in_process_context(bridge):
    """In-process MCP (Claude path) has no session header — keep it working."""
    kt.set_current_chat(5005)
    resp = await _call(bridge, "whoami", {})
    assert resp.status == 200
    assert _body(resp)["result"]["chat"] == 5005


@pytest.mark.asyncio
async def test_missing_session_and_no_context_is_rejected(bridge):
    """No session and no in-process chat → refuse rather than guess a recipient."""
    resp = await _call(bridge, "whoami", {})
    assert resp.status == 409
    assert bridge.delivered == []


@pytest.mark.asyncio
async def test_session_survives_later_chat_switch(bridge):
    """A long-running tool call keeps its own chat even as others speak."""
    session = issue_session(1001)
    kt.set_current_chat(9999)
    resp = await _call(bridge, "whoami", {}, session=session)
    assert _body(resp)["result"]["chat"] == 1001


@pytest.mark.asyncio
async def test_concurrent_chats_each_get_their_own(bridge):
    """Interleaved, not sequential — this is how it breaks in production."""
    import asyncio

    sessions = {chat: issue_session(chat) for chat in (1001, 2002, 3003)}
    results = await asyncio.gather(*(
        _call(bridge, "whoami", {}, session=handle)
        for chat, handle in sessions.items()
    ))
    delivered = [_body(r)["result"]["chat"] for r in results]
    assert delivered == [1001, 2002, 3003]


def test_issue_session_is_unforgeable():
    a = issue_session(1001)
    b = issue_session(1001)
    assert a != b, "sessions must not be guessable from the chat id"
    assert str(1001) not in a
