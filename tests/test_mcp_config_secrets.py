"""#19 — MCP server secrets must never reach the CLI's argv.

The assertions run against the command the SDK actually builds, not against
our own options object: the leak lived in the SDK's serialisation step, so a
test that stops at `options.mcp_servers` would stay green while `ps` still
showed the password.
"""

import json

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

import config
from claude_session import ClaudeSession, write_external_mcp_config


PASSWORD = "sup3r-secret-value-not-in-argv"
API_KEY = "sk-or-v1-000000000000000000000000"

SERVERS = {
    "kesha": {"type": "sdk", "name": "kesha", "instance": object()},
    "yougile": {
        "type": "stdio",
        "command": "/usr/bin/yougile",
        "env": {"YOUGILE_PASSWORD": PASSWORD, "YOUGILE_EMAIL": "maxim@example.com"},
    },
    "websearch": {
        "type": "stdio",
        "command": "node",
        "args": ["/opt/websearch/index.js"],
        "env": {"OPENROUTER_API_KEY": API_KEY},
    },
}


def build_argv(tmp_path, servers):
    session = ClaudeSession(
        cwd=".",
        model=config.MODEL,
        mcp_servers=servers,
        session_file=tmp_path / "session",
    )
    options = session._make_options()
    transport = SubprocessCLITransport(prompt="hi", options=options)
    transport._cli_path = "/usr/bin/claude"
    return transport._build_command()


@pytest.fixture(autouse=True)
def _external_config_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "claude_session.EXTERNAL_MCP_CONFIG", tmp_path / "mcp-external.json"
    )


def test_secret_values_never_appear_in_argv(tmp_path):
    argv = " ".join(build_argv(tmp_path, SERVERS))
    assert PASSWORD not in argv
    assert API_KEY not in argv
    assert "maxim@example.com" not in argv


def test_external_servers_are_handed_over_as_a_file(tmp_path):
    argv = build_argv(tmp_path, SERVERS)
    paths = [
        argv[i + 1]
        for i, a in enumerate(argv[:-1])
        if a == "--mcp-config" and not argv[i + 1].startswith("{")
    ]
    assert len(paths) == 1, f"expected exactly one file config, got {paths}"

    written = json.loads((tmp_path / "mcp-external.json").read_text())["mcpServers"]
    assert sorted(written) == ["websearch", "yougile"]
    assert written["yougile"]["env"]["YOUGILE_PASSWORD"] == PASSWORD


def test_in_process_server_still_travels_in_argv(tmp_path):
    """The SDK routes SDK-server tool calls by `instance`, which only a dict carries.

    Drop `kesha` from `options.mcp_servers` and every Kesha tool disappears,
    so the inline config must survive the fix.
    """
    argv = build_argv(tmp_path, SERVERS)
    inline = [
        argv[i + 1]
        for i, a in enumerate(argv[:-1])
        if a == "--mcp-config" and argv[i + 1].startswith("{")
    ]
    assert len(inline) == 1
    assert json.loads(inline[0]) == {"mcpServers": {"kesha": {"type": "sdk", "name": "kesha"}}}


def test_sdk_entry_cannot_smuggle_a_secret_into_argv(tmp_path):
    """The inline entry is rebuilt, not copied, so extra fields cannot ride along.

    The SDK strips only `instance` before serialising; any other key on an SDK
    config would be JSON-dumped into argv exactly like the leak this fixes.
    """
    servers = dict(SERVERS)
    servers["kesha"] = {
        "type": "sdk",
        "name": "kesha",
        "instance": object(),
        "env": {"SMUGGLED": PASSWORD},
    }
    argv = build_argv(tmp_path, servers)
    inline = next(a for a in argv if a.startswith('{"mcpServers"'))
    assert json.loads(inline) == {"mcpServers": {"kesha": {"type": "sdk", "name": "kesha"}}}
    assert PASSWORD not in " ".join(argv)


def test_write_does_not_follow_a_planted_symlink(tmp_path):
    """A predictable temp name lets any same-user process redirect the write.

    The old fixed `<name>.tmp` was opened with O_TRUNC, which follows a symlink
    and clobbers whatever it points at — and leaves the config's mode alone.
    """
    victim = tmp_path / "victim"
    victim.write_text("do not touch")
    target = tmp_path / "mcp-external.json"
    (tmp_path / "mcp-external.json.tmp").symlink_to(victim)

    write_external_mcp_config({"yougile": SERVERS["yougile"]}, target)

    assert victim.read_text() == "do not touch"
    assert json.loads(target.read_text())["mcpServers"]["yougile"]["env"][
        "YOUGILE_PASSWORD"
    ] == PASSWORD
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_external_config_file_is_owner_only(tmp_path):
    path = tmp_path / "nested" / "mcp-external.json"
    write_external_mcp_config({"yougile": SERVERS["yougile"]}, path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_returned_path_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    returned = write_external_mcp_config({"a": {"command": "x"}}, tmp_path / "cfg.json")
    # The CLI resolves --mcp-config against ITS cwd (WORK_DIR), not the bot's.
    assert returned.startswith("/")


def test_no_external_servers_means_no_file_config(tmp_path):
    argv = build_argv(tmp_path, {"kesha": SERVERS["kesha"]})
    file_configs = [
        argv[i + 1]
        for i, a in enumerate(argv[:-1])
        if a == "--mcp-config" and not argv[i + 1].startswith("{")
    ]
    assert file_configs == []
    assert not (tmp_path / "mcp-external.json").exists()
