"""T3a — bridge transport + authentication. Negative cases are the point here."""

import asyncio

import pytest
from aiohttp import web

import tool_bridge
from tool_bridge import ToolBridge, issue_token


@pytest.fixture
def bridge():
    calls = []

    async def echo(args):
        calls.append(args)
        return {"ok": True, "got": args}

    b = ToolBridge(token="secret-token", resolve_chat=lambda: 12345)
    b.register("echo", echo, allowed_args={"x", "msg"})
    b.calls = calls  # type: ignore[attr-defined]
    return b


async def _post(bridge: ToolBridge, payload: dict, token: str | None = "secret-token"):
    """Drive handle() directly with a stub request — no socket needed for auth logic."""

    class _Req:
        def __init__(self):
            self.headers = {} if token is None else {"X-Kesha-Bridge-Token": token}

        async def json(self):
            if payload is None:
                raise ValueError("no json")
            return payload

    return await bridge.handle(_Req())  # type: ignore[arg-type]


def _body(response: web.Response) -> dict:
    import json

    return json.loads(response.body)


def test_empty_token_rejected_at_construction():
    with pytest.raises(ValueError, match="token must not be empty"):
        ToolBridge(token="", resolve_chat=lambda: 1)


@pytest.mark.asyncio
async def test_call_without_token_is_rejected(bridge):
    resp = await _post(bridge, {"tool": "echo", "args": {}}, token=None)
    assert resp.status == 401
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_call_with_wrong_token_is_rejected(bridge):
    resp = await _post(bridge, {"tool": "echo", "args": {}}, token="not-the-token")
    assert resp.status == 401
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_unauthenticated_call_does_not_reveal_tool_existence(bridge):
    """Unknown tool + bad token must look the same as known tool + bad token."""
    known = await _post(bridge, {"tool": "echo", "args": {}}, token="bad")
    unknown = await _post(bridge, {"tool": "nope", "args": {}}, token="bad")
    assert known.status == unknown.status == 401
    assert _body(known) == _body(unknown)


@pytest.mark.asyncio
async def test_non_ascii_token_does_not_crash(bridge):
    """compare_digest raises TypeError on non-ASCII str — must be handled."""
    resp = await _post(bridge, {"tool": "echo", "args": {}}, token="токен")
    assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "chat_id",
        "chatid",
        "CHAT_ID",
        "Chat",
        "chatId",
        "ChatId",
        " chat_id",          # leading space
        "chat_id ",          # trailing space
        "chat-id",           # hyphen
        "сhat_id",      # Cyrillic 'с' homoglyph
        "chat_id​",     # zero-width space
        "​chat_id",
        "chat​_id",
        "\tchat_id",
        "unknown_key",       # anything unexpected, not just chat_id
        "path",              # valid for another tool, not for this one
    ],
)
async def test_unwhitelisted_argument_is_rejected(bridge, key):
    """Whitelist beats blacklist: every spelling and every unexpected key is refused."""
    resp = await _post(bridge, {"tool": "echo", "args": {key: 999}})
    assert resp.status == 400
    assert "not accepted" in _body(resp)["error"]
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_chat_id_smuggled_alongside_valid_arg_is_rejected(bridge):
    resp = await _post(bridge, {"tool": "echo", "args": {"x": 1, " chat_id": 999}})
    assert resp.status == 400
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_handler_receives_normalized_keys(bridge):
    """A handler must never see the caller's spelling — only canonical names."""
    resp = await _post(bridge, {"tool": "echo", "args": {"MSG": "hi"}})
    assert resp.status == 200
    assert bridge.calls == [{"msg": "hi"}]


@pytest.mark.asyncio
async def test_duplicate_after_normalization_is_rejected(bridge):
    resp = await _post(bridge, {"tool": "echo", "args": {"msg": "a", "MSG": "b"}})
    assert resp.status == 400
    assert "duplicate" in _body(resp)["error"]
    assert bridge.calls == []


def test_tool_cannot_declare_non_ascii_argument(bridge):
    with pytest.raises(ValueError, match="invalid argument"):
        bridge.register("bad", lambda args: None, allowed_args={"пут"})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("chat_id", "chat_id"),
        (" chat_id ", "chat_id"),
        ("CHAT-ID", "chat_id"),
        ("chat_id​", "chat_id"),
        ("сhat_id", None),   # Cyrillic — never a valid name
        (123, None),              # non-str key
    ],
)
def test_normalize_arg_name(raw, expected):
    from tool_bridge import normalize_arg_name

    assert normalize_arg_name(raw) == expected


@pytest.mark.asyncio
async def test_valid_call_succeeds(bridge):
    resp = await _post(bridge, {"tool": "echo", "args": {"x": 1}})
    assert resp.status == 200
    assert _body(resp)["result"] == {"ok": True, "got": {"x": 1}}
    assert bridge.calls == [{"x": 1}]


@pytest.mark.asyncio
async def test_unknown_tool_is_404_when_authenticated(bridge):
    resp = await _post(bridge, {"tool": "missing", "args": {}})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_missing_chat_context_is_rejected():
    async def handler(args):
        raise AssertionError("must not run without a chat context")

    b = ToolBridge(token="t", resolve_chat=lambda: None)
    b.register("echo", handler)

    class _Req:
        headers = {"X-Kesha-Bridge-Token": "t"}

        async def json(self):
            return {"tool": "echo", "args": {}}

    resp = await b.handle(_Req())  # type: ignore[arg-type]
    assert resp.status == 409


@pytest.mark.asyncio
async def test_args_must_be_object(bridge):
    resp = await _post(bridge, {"tool": "echo", "args": ["not", "a", "dict"]})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_tool_exception_becomes_500_with_class_name(bridge):
    async def boom(args):
        raise RuntimeError("kaboom")

    bridge.register("boom", boom)
    resp = await _post(bridge, {"tool": "boom", "args": {}})
    assert resp.status == 500
    assert "RuntimeError: kaboom" in _body(resp)["error"]


def test_duplicate_tool_registration_rejected(bridge):
    with pytest.raises(ValueError, match="already registered"):
        bridge.register("echo", lambda args: None)  # type: ignore[arg-type]


def test_issue_token_is_random_and_persisted(monkeypatch):
    monkeypatch.delenv(tool_bridge.TOKEN_ENV, raising=False)
    first = issue_token()
    assert len(first) >= 32
    # Once issued it is reused from the environment (the MCP subprocess inherits it).
    assert issue_token() == first

    monkeypatch.delenv(tool_bridge.TOKEN_ENV, raising=False)
    assert issue_token() != first


@pytest.mark.asyncio
async def test_socket_is_owner_only(tmp_path, monkeypatch):
    """Filesystem permissions narrow the surface before auth runs."""
    import os

    sock = tmp_path / "bridge.sock"
    monkeypatch.setattr(tool_bridge, "SOCKET_PATH", sock)

    b = ToolBridge(token="t", resolve_chat=lambda: 1)
    b.register("echo", lambda args: asyncio.sleep(0, result={}))
    await b.start()
    try:
        assert sock.exists()
        assert os.stat(sock).st_mode & 0o777 == 0o600
    finally:
        await b.stop()
    assert not sock.exists()
