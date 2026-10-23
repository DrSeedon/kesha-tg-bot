"""Verify the timeout and liveness tests fail when their protections are removed.

Run only when no other test process is using the checkout. Always restore source.
"""

import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys


task_dir = Path(__file__).resolve().parent
root = task_dir.parents[2]
source_path = root / "codex_session.py"
original = source_path.read_text()
assert importlib.metadata.version("claude-agent-sdk") == "0.2.158"
assert importlib.metadata.version("mcp") == "1.28.1"


def run(label, paths, expected):
    with (task_dir / f"{label}.txt").open("w") as output:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "--show-capture=no", *paths],
            cwd=root, stdout=output, stderr=subprocess.STDOUT, timeout=40,
        )
    print(f"{label}: exit={result.returncode}, expected={expected}", flush=True)
    assert result.returncode == expected


mutations = [
    (
        "mutation-timeout-config",
        'for key in ("tool_timeout_sec", "startup_timeout_sec"):\n                if key not in cfg:',
        'for key in ():\n                if key not in cfg:',
        ["tests/test_codex_session.py::test_mcp_timeouts_reach_toml_from_workdir_config"],
    ),
    (
        "mutation-open-tools",
        "open_tools.add(item_id)",
        "open_tools.discard(item_id)",
        ["tests/test_long_tool_liveness.py", "-k", "hub_image_silence_budget and codex and False"],
    ),
]
try:
    for label, before, after, paths in mutations:
        assert original.count(before) == 1
        source_path.write_text(original.replace(before, after))
        os.utime(source_path, None)
        try:
            run(label, paths, 1)
        finally:
            source_path.write_text(original)
            os.utime(source_path, None)
finally:
    assert source_path.read_text() == original
    print("Source restored byte-for-byte.", flush=True)

run("regressions-restored", [
    "tests/test_codex_session.py::test_mcp_timeouts_reach_toml_from_workdir_config",
    "tests/test_codex_session.py::test_open_tools_follow_ids_and_ignore_other_turns",
    "tests/test_long_tool_liveness.py", "tests/test_file_access.py",
], 0)
