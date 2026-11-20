"""Bounded structural telemetry. Tool values and free text are never persisted."""

from dataclasses import dataclass, field
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
from uuid import uuid4


logger = logging.getLogger(__name__)
_ACTIVE = {}
KNOWN_SERVERS = frozenset({"mailru", "gmail", "yandex-direct", "browser", "kesha"})
IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
# Only schema-declared keys in this vocabulary may leave an MCP packet.
PORTS = frozenset({
    "folder", "query", "limit", "max_results", "page_token", "message_id", "ids", "account",
    "date_from", "date_to", "campaign_ids", "goal_ids", "attribution_models", "include_vat",
    "fields", "selector", "selectors", "tab_id", "url", "request", "window",
})
TOOLS = {
    "mailru": frozenset({"mail_folders", "mail_count_all", "mail_search", "mail_read", "mail_unread", "mail_list"}),
    "gmail": frozenset({"search_gmail_messages", "get_gmail_message_content", "get_gmail_messages_content_batch",
                        "list_gmail_labels"}),
    "yandex-direct": frozenset({"get_statistics", "get_campaign_spend", "get_search_queries"}),
    "browser": frozenset({"list_tabs", "snapshot", "get_page_info"}),
    "kesha": frozenset({"capsule_run"}),
}


@dataclass
class Invocation:
    id: str = field(default_factory=lambda: uuid4().hex)
    events: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)
    reason: str | None = None
    usage_ref: int | None = None
    terminal_ok: bool = False


def begin_turn(chat):
    previous = _ACTIVE.get(chat)
    if previous:
        previous.reason = "overlap"
    turn = Invocation(reason="overlap" if previous else None)
    _ACTIVE[chat] = turn
    return turn


def current_turn(chat):
    return _ACTIVE.get(chat)


def mark(chat, reason):
    turn = current_turn(chat)
    if turn:
        turn.reason = reason


def terminal(chat, usage_ref, ok):
    turn = current_turn(chat)
    if turn:
        turn.usage_ref = usage_ref
        turn.terminal_ok = ok


def finish_turn(chat, turn):
    if current_turn(chat) is turn:
        _ACTIVE.pop(chat)
    if turn.pending:
        turn.reason = "incomplete"
    if not turn.terminal_ok or not turn.usage_ref:
        turn.reason = turn.reason or "unverified_terminal"
    path = os.environ.get("KESHA_SKILLCACHE_TRACE_DB")
    if path and (turn.events or turn.pending):
        try:
            _persist(Path(path), turn)
        except (OSError, sqlite3.Error):
            logger.warning("SkillCache trace journal unavailable")


def _persist(path, turn):
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    try:
        status = os.fstat(fd)
        from stat import S_ISREG

        if not S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
            raise PermissionError("unsafe SkillCache trace path")
    finally:
        os.close(fd)
    conn = sqlite3.connect(path, timeout=0.3)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS invocation (
                id TEXT PRIMARY KEY, usage_ref INTEGER, eligible INTEGER NOT NULL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS event (
                invocation_id TEXT NOT NULL REFERENCES invocation(id),
                seq INTEGER NOT NULL, server TEXT NOT NULL, tool TEXT NOT NULL,
                args_schema_bound TEXT NOT NULL, result_summary TEXT NOT NULL, is_error INTEGER NOT NULL,
                PRIMARY KEY(invocation_id, seq)
            );
        """)
        with conn:
            conn.execute("INSERT INTO invocation VALUES (?,?,?,?)",
                         (turn.id, turn.usage_ref, int(not turn.reason), turn.reason))
            for seq, server, tool, args, result, error in sorted(turn.events):
                conn.execute("INSERT INTO event VALUES (?,?,?,?,?,?,?)",
                             (turn.id, seq, server, tool, json.dumps(args), json.dumps(result), int(error)))
    finally:
        conn.close()


def arg_shape(arguments, properties):
    if not isinstance(arguments, dict) or not set(arguments) <= properties:
        return None
    primitive = {str: "string", int: "integer", bool: "boolean", float: "number",
                 dict: "object", list: "array", type(None): "null"}
    return {key: primitive.get(type(value), "unknown")
            for key, value in arguments.items() if key in PORTS}


def begin_event(chat, server, tool, shape, *, schema_digest=None):
    turn = current_turn(chat)
    if not turn or not os.environ.get("KESHA_SKILLCACHE_TRACE_DB"):
        return None
    if tool not in TOOLS.get(server, ()) or not isinstance(shape, dict):
        turn.reason = "unprojectable_tool"
        return None
    if schema_digest is not None and (not isinstance(schema_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", schema_digest)):
        turn.reason = "unverified_schema"
        return None
    allowed = {"string", "integer", "boolean", "number", "object", "array", "null"}
    if not shape.keys() <= PORTS or any(not isinstance(x, str) or x not in allowed for x in shape.values()):
        turn.reason = "unprojectable_tool"
        return None
    if len(turn.events) + len(turn.pending) >= 512:
        turn.reason = "trace_limit"
        return None
    if turn.pending:
        turn.reason = "overlap"
    ticket = uuid4().hex
    seq = len(turn.events) + len(turn.pending) + 1
    turn.pending[ticket] = (seq, server, tool, {"ports": shape, "schema_digest": schema_digest})
    return turn.id, ticket


def finish_event(chat, invocation, ticket, *, blocks, error):
    turn = current_turn(chat)
    if not turn or turn.id != invocation or ticket not in turn.pending:
        return False
    if type(blocks) is not int or not 0 <= blocks <= 500 or type(error) is not bool:
        turn.reason = "unprojectable_result"
        return False
    event = turn.pending.pop(ticket)
    turn.events.append((*event, {"content_blocks": blocks}, error))
    if error:
        turn.reason = "tool_failed"
    return True


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=0.35)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


_DEFAULT_TRACE_FILE = Path("/opt/kesha-bot/storage/sessions/skillcache-trace-antigravity.json")


def _load_private_trace_config():
    cfg_path = Path(os.environ.get("KESHA_SKILLCACHE_TRACE_FILE") or _DEFAULT_TRACE_FILE)
    try:
        fd = os.open(cfg_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        from stat import S_ISREG
        st = os.fstat(fd)
        if not S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return {}
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.loads(stream.read(4096))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)


class ProxyTrace:
    def __init__(self, server):
        self.server = server if server in KNOWN_SERVERS else "unknown"
        self.path = os.environ.get("KESHA_SKILLCACHE_TRACE_SOCKET")
        self.token = os.environ.get("KESHA_SKILLCACHE_TRACE_HANDLE")
        self.schema_dir = os.environ.get("KESHA_SKILLCACHE_SCHEMA_DIR")
        if not self.path or not self.token:
            file_cfg = _load_private_trace_config()
            self.path = self.path or file_cfg.get("socket") or None
            self.token = self.token or file_cfg.get("handle") or None
            if not self.schema_dir and file_cfg.get("schema_dir"):
                self.schema_dir = str(file_cfg["schema_dir"])
        self.schemas = {}

    def _send(self, payload):
        if not self.path or not self.token:
            return None
        client = _UnixHTTP(self.path)
        try:
            client.request("POST", "/skillcache/trace", json.dumps(payload),
                           {"X-Kesha-SkillCache-Trace": self.token, "Content-Type": "application/json"})
            response = client.getresponse()
            if response.status == 200:
                return json.loads(response.read(1024))
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            client.close()
        return None

    def list_tools(self, raw, transformed, *, first_page=True):
        if first_page:
            self.schemas.clear()
        for item in transformed:
            name = item.get("name", "") if isinstance(item, dict) else ""
            if name not in TOOLS.get(self.server, ()):
                continue
            before = raw.get(name, {})
            shape = item.get("inputSchema", {})
            properties = shape.get("properties", {}) if isinstance(shape, dict) else {}
            schema = [shape, before.get("outputSchema"), item.get("outputSchema")]
            normalized = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
            self.schemas[name] = (set(properties), hashlib.sha256(normalized).hexdigest())
        if self.schema_dir and self.server != "unknown":
            self._publish_schema_digests()

    def _publish_schema_digests(self):
        directory = Path(self.schema_dir)
        temporary = None
        try:
            stat = directory.lstat()
            if (directory.is_symlink() or not directory.is_dir() or stat.st_uid != os.getuid()
                    or stat.st_mode & 0o077):
                return
            target = directory / f"{self.server}.json"
            data = {name: checksum for name, (_, checksum) in self.schemas.items()}
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, sort_keys=True)
            temporary.replace(target)
        except OSError:
            logger.warning("SkillCache schema journal unavailable")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def begin(self, params):
        tool = params.get("name", "")
        if not isinstance(tool, str) or not IDENT.fullmatch(tool):
            return None
        properties, schema_hash = self.schemas.get(tool, (None, ""))
        shape = arg_shape(params.get("arguments", {}), properties) if properties is not None else None
        response = self._send({"action": "begin", "server": self.server, "tool": tool,
                               "shape": shape, "schema_digest": schema_hash})
        return response.get("ticket") if response else None

    def finish(self, ticket, msg):
        result = msg.get("result")
        result = result if isinstance(result, dict) else {}
        content = result.get("content", [])
        blocks = len(content) if isinstance(content, list) else 501
        self._send({"action": "finish", "ticket": ticket, "blocks": blocks,
                    "error": "error" in msg or bool(result.get("isError"))})




def wrap_stdio_servers(servers, runtime, chat, work_dir):
    """Use the same trusted launcher as ACP with the native schema preserved."""
    if runtime not in {"claude", "codex"} or not os.environ.get("KESHA_SKILLCACHE_TRACE_DB"):
        return servers
    import sys
    from kesha_mcp_launcher import load_trusted_server_config
    from tool_bridge import SOCKET_PATH, issue_session

    wrapped = dict(servers)
    handle = None
    for name in KNOWN_SERVERS - {"kesha"}:
        spec = servers.get(name)
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        try:
            source = load_trusted_server_config(name, home=Path.home(), work_dir=str(work_dir))
        except (KeyError, OSError, ValueError):
            continue
        if any(source.get(key, empty) != spec.get(key, empty)
               for key, empty in (("command", ""), ("args", []), ("env", {}))):
            continue
        if handle is None:
            handle = issue_session(chat, ttl=86400, runtime=f"skillcache-trace:{runtime}")
        wrapped[name] = {
            **spec, "command": sys.executable,
            "args": [str(Path(__file__).with_name("kesha_mcp_launcher.py")), name, "--trace-only"],
            "env": {"KESHA_SKILLCACHE_TRACE_SOCKET": str(SOCKET_PATH),
                    "KESHA_SKILLCACHE_TRACE_HANDLE": handle,
                    "KESHA_SKILLCACHE_SCHEMA_DIR": os.environ.get("KESHA_SKILLCACHE_SCHEMA_DIR", "")},
        }
    return wrapped
