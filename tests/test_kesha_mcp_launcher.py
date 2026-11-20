from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kesha_mcp_launcher as launcher
from antigravity_session import AntigravitySession
from runtime_registry import RuntimeBuildContext


def test_build_child_env_strips_proxy_and_sets_xdg(tmp_path):
    cfg = {
        "command": "/usr/bin/python3",
        "args": ["srv.py"],
        "env": {
            "GOOGLE_OAUTH_CLIENT_SECRET": "sec-123",
            "HTTP_PROXY": "http://127.0.0.1:9999",
            "https_proxy": "http://127.0.0.1:8888",
            "ALL_PROXY": "socks5://127.0.0.1:1080",
            "GRPC_PROXY": "http://bad",
        },
    }
    env = launcher.build_child_env(cfg, home=tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / ".config")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / ".cache")
    assert env["XDG_DATA_HOME"] == str(tmp_path / ".local" / "share")
    assert env["FASTMCP_SHOW_CLI_BANNER"] == "false"
    assert env["NO_COLOR"] == "1"
    assert env["GOOGLE_OAUTH_CLIENT_SECRET"] == "sec-123"
    for pvar in ("HTTP_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "GRPC_PROXY"):
        assert pvar not in env


def test_sanitize_fastmcp_initialize_tools_and_call():
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "extensions": {"io.modelcontextprotocol/ui": {}},
            "capabilities": {
                "tools": {"listChanged": True},
                "extensions": {"io.modelcontextprotocol/ui": {}},
            },
            "serverInfo": {"name": "mailru", "version": "1.0"},
        },
    }
    sanitized_init = launcher.sanitize_mcp_message(init_msg, "initialize")
    assert "extensions" not in sanitized_init["result"]
    assert "extensions" not in sanitized_init["result"]["capabilities"]

    tools_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {
                    "name": "mail_list",
                    "description": "List",
                    "inputSchema": {"type": "object", "properties": {}},
                    "outputSchema": {"type": "object", "x-fastmcp-wrap-result": True},
                    "_meta": {"fastmcp": {"tags": []}},
                    "annotations": {"readOnlyHint": True},
                }
            ]
        },
    }
    sanitized_tools = launcher.sanitize_mcp_message(tools_msg, "tools/list")
    t0 = sanitized_tools["result"]["tools"][0]
    assert "outputSchema" not in t0
    assert "_meta" not in t0
    assert t0["name"] == "mail_list"
    assert t0["annotations"] == {"readOnlyHint": True}

    call_msg = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "structuredContent": {"result": "ok"},
            "_meta": {"fastmcp": {}},
        },
    }
    sanitized_call = launcher.sanitize_mcp_message(call_msg, "tools/call")
    assert "_meta" not in sanitized_call["result"]
    assert "structuredContent" not in sanitized_call["result"]
    assert sanitized_call["result"]["content"] == [{"type": "text", "text": '{"result": "ok"}'}]


def test_load_trusted_server_config_checks_owner_and_rejects_recursive(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    mcp_json = work / ".mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "gmail": {
                "command": "/home/kesha/.claude/mcp-servers/gmail-mcp/venv/bin/workspace-mcp",
                "args": ["--tools", "gmail"],
                "env": {"GOOGLE_OAUTH_CLIENT_ID": "abc"},
            },
            "bad": {
                "command": sys.executable,
                "args": ["/opt/kesha-bot/kesha_mcp_launcher.py", "gmail"],
            },
        }
    }), encoding="utf-8")
    mcp_json.chmod(0o600)

    cfg = launcher.load_trusted_server_config("gmail", home=tmp_path, work_dir=str(work), bot_dir=tmp_path)
    assert cfg["env"]["GOOGLE_OAUTH_CLIENT_ID"] == "abc"

    with pytest.raises(ValueError, match="recursive"):
        launcher.load_trusted_server_config("bad", home=tmp_path, work_dir=str(work), bot_dir=tmp_path)


def test_antigravity_session_wraps_all_external_mcps_via_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("KESHA_BRIDGE_AUTH_TOKEN", "test-bridge-token")
    import tool_bridge
    monkeypatch.setattr(tool_bridge, "TOKEN_ENV", "KESHA_BRIDGE_AUTH_TOKEN")
    monkeypatch.setattr(tool_bridge, "issue_session", lambda *a, **kw: "bridge-sess-1")
    monkeypatch.setattr(tool_bridge, "session_valid_for", lambda *a, **kw: False)
    monkeypatch.setattr(tool_bridge, "revoke_session", lambda *a, **kw: None)

    mcps = {
        "kesha": {"type": "sdk", "name": "kesha"},
        "yougile": {"command": "/usr/bin/python3", "args": ["y.py"], "env": {"YOUGILE_API_KEY": "sec"}},
        "mailru": {"command": "/usr/bin/python3", "args": ["m.py"]},
        "gmail": {"command": "/usr/bin/workspace-mcp", "args": [], "env": {"GOOGLE_OAUTH_CLIENT_SECRET": "s"}},
        "websearch": {"command": "node", "args": ["w.js"], "env": {"OPENROUTER_API_KEY": "or"}},
        "ozon": {"command": "ssh", "args": ["ozon@host"]},
        "gpb": {"command": "/usr/bin/python3", "args": ["g.py"]},
        "hub-images": {"command": "uv", "args": ["run"], "env": {"MULTICODE_MCP_TOKEN_FILE": "/tmp/tok"}},
    }
    ctx = RuntimeBuildContext(
        chat_id=12345,
        cwd=str(tmp_path),
        model="gemini-4-neon-medium",
        system_prompt="test",
        mcp_servers=mcps,
        session_file=tmp_path / "12345.antigravity",
    )
    sess = AntigravitySession(ctx)
    acp_list = sess._acp_mcp_servers()
    names = [item["name"] for item in acp_list]
    assert names == ["kesha", "yougile", "mailru", "gmail", "websearch", "ozon", "gpb", "hub-images"]
    for item in acp_list:
        if item["name"] == "kesha":
            assert Path(item["args"][0]).name == "kesha_mcp_proxy.py"
            assert set(item["env"]) == {"KESHA_BRIDGE_AUTH_FILE"}
        else:
            assert item["args"][1] == item["name"]
            assert Path(item["args"][0]).name == "kesha_mcp_launcher.py"
            assert item["env"] == {}

    asyncio.run(sess.apply_mcp_servers(mcps))


def test_mailru_mail_send_535_hint_and_attachment(tmp_path, monkeypatch):
    import importlib
    import smtplib
    import types

    fake_fastmcp = types.ModuleType("fastmcp")
    class FakeFastMCP:
        def __init__(self, name):
            self.name = name
        def tool(self):
            return lambda fn: fn
    fake_fastmcp.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp)
    monkeypatch.syspath_prepend("/home/kesha/.claude/mcp-servers/mailru-mcp")
    sys.modules.pop("server", None)
    mailru_srv = importlib.import_module("server")

    att = tmp_path / "note.txt"
    att.write_text("hello", encoding="utf-8")

    # 1. Missing attachment rejected cleanly
    res_missing = json.loads(mailru_srv.mail_send("a@b.ru", "Subj", "Body", attachments=[str(tmp_path / "nope.txt")]))
    assert res_missing["ok"] is False
    assert "Attachment not found" in res_missing["error"]

    # 2. SMTP 535 returns clear hint without exposing password
    class FakeSmtp535:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, user, pw):
            raise smtplib.SMTPAuthenticationError(535, b"Application password is REQUIRED")

    with patch.object(mailru_srv.smtplib, "SMTP_SSL", FakeSmtp535):
        res_535 = json.loads(mailru_srv.mail_send("a@b.ru", "Subj", "Body", attachments=[str(att)]))
    assert res_535["ok"] is False
    assert "send_gmail_message" in res_535["error"]
    assert mailru_srv.PASSWORD not in res_535["error"]

    # 3. Happy path sends message and appends to Sent folder found via \Sent flag
    sent_calls = []
    class FakeSmtpOk:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, user, pw):
            pass
        def send_message(self, msg, to_addrs=None):
            sent_calls.append((msg["Subject"], to_addrs, [p.get_filename() for p in msg.iter_attachments()]))

    fake_imap = MagicMock()
    fake_imap.list.return_value = ("OK", [b'(\\Sent) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"'])
    with (
        patch.object(mailru_srv.smtplib, "SMTP_SSL", FakeSmtpOk),
        patch.object(mailru_srv, "connect", return_value=fake_imap),
    ):
        res_ok = json.loads(mailru_srv.mail_send("a@b.ru", "Test", "Hi", cc="c@d.ru", attachments=[str(att)]))
    assert res_ok["ok"] is True
    assert res_ok["attachments"] == ["note.txt"]
    assert res_ok["saved_to_sent"] is True
    assert sent_calls[0][1] == ["a@b.ru", "c@d.ru"]
    assert sent_calls[0][2] == ["note.txt"]
    fake_imap.append.assert_called_once()


def test_antigravity_session_rotates_on_mcp_signature_change_with_handoff(tmp_path, monkeypatch):
    sfile = tmp_path / "720740564.antigravity"
    sfile.write_text(json.dumps({
        "sessionId": "old-sid-3mcp",
        "token": "t" * 64,
        "context": {"totalTokens": 100},
        "framing": "old-framing",
        "mcpSignature": "old-3mcp-sig",
    }), encoding="utf-8")
    mcps = {"mailru": {"command": "/usr/bin/python3", "args": ["m.py"]}}
    ctx = RuntimeBuildContext(
        chat_id=720740564,
        cwd=str(tmp_path),
        model="gemini-4-neon-medium",
        system_prompt="test",
        mcp_servers=mcps,
        session_file=sfile,
    )
    sess = AntigravitySession(ctx)
    closed = []
    seen_open = []

    class FakeClient:
        attached = None
        def __init__(self):
            self.submitted = asyncio.Event()
        async def request(self, method, params, *, timeout=None, on_write=None):
            if method == "session/open":
                seen_open.append(params)
                return {"sessionId": None, "audio": False}
            if method == "session/prompt":
                on_write()
                self.submitted.set()
                return {"stopReason": "end_turn"}
            return {}
        async def close(self):
            pass

    monkeypatch.setattr(sess, "_close_native", lambda st: asyncio.sleep(0, result=closed.append(st["sessionId"])))
    monkeypatch.setattr(sess, "_build_mcp_handoff", lambda: asyncio.sleep(0, result="handoff-context-720740564"))
    import antigravity_session as ag_mod
    monkeypatch.setattr(ag_mod.MuxClient, "connect", classmethod(lambda cls, allow_fallback=True: asyncio.sleep(0, result=FakeClient())))

    chunks = []
    async def run():
        async for ch in sess.send_message("проверь почту", interactive_user=True, origin="telegram"):
            chunks.append(ch)
    asyncio.run(run())
    assert closed == ["old-sid-3mcp"]
    assert seen_open and seen_open[0]["sessionId"] is None
    assert sess._state["mcpSignature"] == sess._mcp_signature(sess._acp_mcp_servers())
