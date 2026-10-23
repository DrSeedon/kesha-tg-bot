"""Offline inspection of the actual bundled CLI; no auth or generation calls."""

import importlib.metadata
import os
from pathlib import Path
import re
import subprocess

import claude_agent_sdk


task_dir = Path(__file__).resolve().parent
sdk_root = Path(claude_agent_sdk.__file__).parent
binary = sdk_root / "_bundled" / "claude"
version = subprocess.run(
    [str(binary), "--version"], check=True, capture_output=True, text=True,
    timeout=30,
    env={**os.environ, "CLAUDE_CONFIG_DIR": str(task_dir / "tmp" / "cli-config")},
).stdout.strip()
print("claude-agent-sdk:", importlib.metadata.version("claude-agent-sdk"))
print("mcp:", importlib.metadata.version("mcp"))
print("Bundled CLI:", version)
assert version == "2.1.280 (Claude Code)"

data = binary.read_bytes()
marker = b"function nr(e){let r=(e?.timeout"
assert data.count(marker) == 1
start = data.index(marker)
end = data.index(b"var Ms=", start)
functions = data[start:end].decode()
print("\nActual CLI hard timeout / idle timeout functions:")
print(functions)
assert "e.timeout>=1000" in functions
assert "a.MCP_TOOL_TIMEOUT??as" in functions
assert 'n===\"stdio\"?Ps:Ts' in functions
assert "Ts=300000,Ps=1800000" in functions
default_pos = data.rfind(b"var as=", 0, start)
default = data[default_pos:data.index(b",", default_pos)].decode()
print("Hard timeout default in the same code region:", default)
assert default == "var as=1e8"
print("Hard timeout default: 100000000 ms = 100000 s")
print("Stdio idle timeout default: 1800000 ms = 1800 s")
print("Both defaults exceed 400 s; no bot-side Claude override needed.")

schema_marker = b"Per-server tool-call timeout in milliseconds. Overrides the MCP_TOOL_TIMEOUT"
schema_start = data.rfind(schema_marker)
schema_end = data.index(b'",Et=', schema_start)
print("\nActual CLI timeout field description:")
print(data[schema_start:schema_end].decode())
assert b"timeout:Et()" in data
print("Stdio schema includes timeout:Et(); timeout is a per-server millisecond budget.")

source = (sdk_root / "_internal" / "transport" / "subprocess_cli.py").read_text()
spawn = re.search(r"process_env = \{[^}]+\}", source)
assert spawn is not None
print("\nActual SDK child environment construction:")
print(spawn.group())
assert "**self._options.env" in spawn.group()
