"""Stdio launcher and FastMCP response sanitizer for Kesha external MCP servers.

Invoked inside `agy-clean-exec` tool-daemon with no secrets in argv or mux env:
    python3 /opt/kesha-bot/kesha_mcp_launcher.py <server_name>
Loads the server configuration from trusted local config files, builds a clean
child environment (without proxy overrides, plus the server's own env from
`.mcp.json`), and strips FastMCP 3.x extensions/outputSchema/_meta fields that
can break strict ACP MCP parsers.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

LAUNCHER_BASENAME = "kesha_mcp_launcher.py"
DEFAULT_WORK_DIR = "/opt/cog-second-brain"
DEFAULT_PATH = "/home/kesha/.local/bin:/usr/local/bin:/usr/bin:/bin"
STRIP_PROXY_VARS = frozenset({
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "GRPC_PROXY",
    "NO_GRPC_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "grpc_proxy",
    "no_grpc_proxy",
})


def _owner_uid() -> int:
    return os.getuid()


def _owner_name() -> str:
    try:
        return pwd.getpwuid(_owner_uid()).pw_name
    except KeyError:
        return os.environ.get("USER") or "kesha"


def _is_trusted_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != _owner_uid():
        return False
    if st.st_mode & 0o002:
        return False
    return True


def _candidate_config_paths(
    *,
    home: Path | None = None,
    work_dir: str | None = None,
    bot_dir: Path | None = None,
) -> list[Path]:
    home_dir = home or Path.home()
    resolved_bot_dir = bot_dir or Path(__file__).resolve().parent
    env_work_dir = work_dir or os.environ.get("KESHA_WORK_DIR") or os.environ.get("WORK_DIR") or ""
    candidates: list[Path] = [
        home_dir / ".claude.json",
        home_dir / ".claude" / "settings.json",
    ]
    seen = {str(p) for p in candidates}
    for d in (env_work_dir, DEFAULT_WORK_DIR, str(resolved_bot_dir)):
        if not d:
            continue
        p = Path(d) / ".mcp.json"
        sp = str(p)
        if sp not in seen:
            candidates.append(p)
            seen.add(sp)
    return candidates


def load_trusted_server_config(
    server_name: str,
    *,
    home: Path | None = None,
    work_dir: str | None = None,
    bot_dir: Path | None = None,
) -> dict:
    if not server_name or server_name == "kesha" or "/" in server_name or "\\" in server_name:
        raise ValueError(f"Invalid external MCP server name: {server_name!r}")
    servers: dict = {}
    for path in _candidate_config_paths(home=home, work_dir=work_dir, bot_dir=bot_dir):
        if not path.exists():
            continue
        if not _is_trusted_file(path):
            sys.stderr.write(f"[kesha_mcp_launcher] skipped untrusted MCP config: {path}\n")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write(f"[kesha_mcp_launcher] unreadable MCP config {path}: {exc}\n")
            continue
        if not isinstance(data, dict):
            continue
        for name, cfg in (data.get("mcpServers") or {}).items():
            if isinstance(name, str) and isinstance(cfg, dict):
                servers.setdefault(name, cfg)
    cfg = servers.get(server_name)
    if not isinstance(cfg, dict):
        raise KeyError(f"External MCP server {server_name!r} not found in trusted configs")
    cmd = cfg.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError(f"MCP server {server_name!r} is missing stdio command")
    args = cfg.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError(f"MCP server {server_name!r} has invalid args")
    if Path(cmd).name == LAUNCHER_BASENAME or any(Path(a).name == LAUNCHER_BASENAME for a in args):
        raise ValueError(f"Refusing recursive launch for {server_name!r}")
    return cfg


def build_child_env(cfg: dict, *, home: Path | None = None) -> dict[str, str]:
    home_dir = home or Path("/home/kesha")
    user = _owner_name()
    env: dict[str, str] = {
        "HOME": str(home_dir),
        "USER": user,
        "LOGNAME": user,
        "PATH": DEFAULT_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": str(home_dir / ".config"),
        "XDG_CACHE_HOME": str(home_dir / ".cache"),
        "XDG_DATA_HOME": str(home_dir / ".local" / "share"),
        "FASTMCP_SHOW_CLI_BANNER": "false",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        env["TMPDIR"] = tmpdir
    for key, val in (cfg.get("env") or {}).items():
        ks = str(key)
        if ks in STRIP_PROXY_VARS:
            continue
        env[ks] = str(val)
    for k in STRIP_PROXY_VARS:
        env.pop(k, None)
    return env


def _simplify_json_schema_node(node: object) -> None:
    if not isinstance(node, dict):
        return
    if "anyOf" in node and isinstance(node["anyOf"], list):
        non_null = [x for x in node["anyOf"] if not (isinstance(x, dict) and x.get("type") == "null")]
        if len(non_null) == 1 and isinstance(non_null[0], dict):
            desc = node.get("description")
            node.pop("anyOf", None)
            node.pop("default", None)
            for k, v in non_null[0].items():
                node.setdefault(k, v)
            if desc is not None:
                node["description"] = desc
    props = node.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            _simplify_json_schema_node(prop)
    items = node.get("items")
    if isinstance(items, dict):
        _simplify_json_schema_node(items)


def _compact_tool_if_oversized(tool: dict, *, limit: int = 1600) -> None:
    if len(json.dumps(tool, ensure_ascii=False)) <= limit:
        return
    schema = tool.get("inputSchema")
    if isinstance(schema, dict):
        _simplify_json_schema_node(schema)
        schema.pop("additionalProperties", None)
    desc = tool.get("description")
    if isinstance(desc, str) and len(desc) > 160:
        tool["description"] = desc.split("\n", 1)[0][:157].rstrip() + "..."
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        for p in props.values():
            if isinstance(p, dict):
                if p.get("default") is None:
                    p.pop("default", None)
                pdesc = p.get("description")
                if isinstance(pdesc, str) and len(pdesc) > 64:
                    p["description"] = pdesc.split("\n", 1)[0][:61].rstrip() + "..."
    if len(json.dumps(tool, ensure_ascii=False)) > limit and isinstance(props, dict):
        for p in props.values():
            if isinstance(p, dict) and isinstance(p.get("description"), str) and len(p["description"]) > 28:
                p["description"] = p["description"][:25].rstrip() + "..."
    if len(json.dumps(tool, ensure_ascii=False)) > limit:
        if isinstance(tool.get("description"), str) and len(tool["description"]) > 80:
            tool["description"] = tool["description"][:77].rstrip() + "..."
        if isinstance(props, dict):
            for p in props.values():
                if isinstance(p, dict):
                    p.pop("description", None)


def sanitize_mcp_message(msg: object, method: str | None = None) -> object:
    if not isinstance(msg, dict):
        return msg
    result = msg.get("result")
    if not isinstance(result, dict):
        return msg

    is_init = method == "initialize" or (
        "protocolVersion" in result and "capabilities" in result
    )
    if is_init:
        result.pop("extensions", None)
        caps = result.get("capabilities")
        if isinstance(caps, dict):
            caps.pop("extensions", None)
        return msg

    is_tools_list = method == "tools/list" or isinstance(result.get("tools"), list)
    if is_tools_list:
        tools = result.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict):
                    tool.pop("outputSchema", None)
                    tool.pop("_meta", None)
                    _compact_tool_if_oversized(tool)
        return msg

    is_tool_call = (
        method == "tools/call"
        or "structuredContent" in result
        or "content" in result
    )
    if is_tool_call:
        result.pop("_meta", None)
        if "structuredContent" in result:
            structured = result.pop("structuredContent")
            content = result.get("content")
            if not isinstance(content, list) or not content:
                text_val = (
                    structured
                    if isinstance(structured, str)
                    else json.dumps(structured, ensure_ascii=False)
                )
                result["content"] = [{"type": "text", "text": text_val}]
        return msg

    return msg


def _is_banner_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if any(ch in stripped for ch in ("╭", "╰", "│", "─", "▄", "▀", "█")):
        return True
    if "FastMCP" in stripped and ("fastmcp" in stripped.lower() or "v3." in stripped or "server" in stripped.lower()):
        return True
    return False


def run_proxy(server_name: str) -> int:
    home_dir = Path("/home/kesha")
    if not home_dir.is_dir():
        home_dir = Path.home()
    cfg = load_trusted_server_config(server_name, home=home_dir)
    child_env = build_child_env(cfg, home=home_dir)
    argv = [cfg["command"], *(cfg.get("args") or [])]

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        bufsize=0,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    pending_lock = threading.Lock()
    pending_methods: dict[object, str] = {}

    def pump_stdin() -> None:
        try:
            while True:
                raw = sys.stdin.buffer.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if text.startswith("{"):
                    try:
                        req = json.loads(text)
                        if isinstance(req, dict):
                            req_id = req.get("id")
                            method = req.get("method")
                            if isinstance(req_id, (str, int)) and isinstance(method, str):
                                with pending_lock:
                                    if len(pending_methods) > 4096:
                                        pending_methods.clear()
                                    pending_methods[req_id] = method
                    except ValueError:
                        pass
                try:
                    proc.stdin.write(raw)
                    proc.stdin.flush()
                except OSError:
                    break
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def pump_stdout() -> None:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if not text.startswith("{"):
                if not _is_banner_noise(text):
                    sys.stderr.write(f"[{server_name}] non-json stdout: {text}\n")
                    sys.stderr.flush()
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                sys.stderr.write(f"[{server_name}] invalid json stdout: {text[:200]}\n")
                sys.stderr.flush()
                continue
            if isinstance(msg, dict):
                method = None
                msg_id = msg.get("id")
                if isinstance(msg_id, (str, int)) and "result" in msg:
                    with pending_lock:
                        method = pending_methods.pop(msg_id, None)
                msg = sanitize_mcp_message(msg, method)
                out = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            else:
                out = raw if raw.endswith(b"\n") else raw + b"\n"
            try:
                sys.stdout.buffer.write(out)
                sys.stdout.buffer.flush()
            except OSError:
                break

    def pump_stderr() -> None:
        while True:
            raw = proc.stderr.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace")
            if _is_banner_noise(text):
                continue
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except OSError:
                break

    def _terminate_child(*_args) -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    prev_term = signal.signal(signal.SIGTERM, _terminate_child)
    prev_int = signal.signal(signal.SIGINT, _terminate_child)

    t_in = threading.Thread(target=pump_stdin, daemon=True)
    t_out = threading.Thread(target=pump_stdout, daemon=True)
    t_err = threading.Thread(target=pump_stderr, daemon=True)
    t_in.start()
    t_out.start()
    t_err.start()

    try:
        t_in.join()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        t_out.join(timeout=2.0)
        t_err.join(timeout=1.0)
        return proc.returncode if proc.returncode is not None else 0
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: kesha_mcp_launcher.py <server_name>\n")
        return 2
    return run_proxy(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
