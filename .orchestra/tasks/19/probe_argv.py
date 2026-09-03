"""#19 acceptance probe: no secret VALUES in argv, all six MCP servers alive.

Runs the real wiring (`bot._load_global_mcp` + `ChatRegistry.build_session`)
without aiogram polling — a second poller would fight the live bot for
`getUpdates`. Everything the fix touches (option building, CLI spawn, tool
routing) is exercised for real.

Prints only variable NAMES and match COUNTS, never a secret value.

    ALLOWED_USERS=<id> TELEGRAM_BOT_TOKEN=<dummy> WORK_DIR=/opt/cog-second-brain \
        .venv/bin/python docs/tasks/19/probe_argv.py
"""

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import bot as bot_module  # noqa: E402
from chat_state import ChatRegistry  # noqa: E402
from config import DEBOUNCE_SEC, MODEL, RUNTIME, WORK_DIR  # noqa: E402
from message_log import get_db as msg_db  # noqa: E402
import compact as _compact  # noqa: E402
import reminders as _reminders  # noqa: E402
import response_stream as _rs  # noqa: E402
from kesha_tools import set_current_chat  # noqa: E402

SECRET_RE = re.compile(r"password|secret|token|key|y0_|sk-or-v1-|ya29\.|AIza", re.I)
PROBE_CHAT = 999000019


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("PPid:"):
                children.setdefault(int(line.split()[1]), []).append(int(entry.name))
                break
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def secret_values() -> dict[str, str]:
    """The exact values the fix must keep out of argv, keyed by their env name.

    Name patterns alone are useless here: `--mcp-config` paths, the ssh option
    `StrictHostKeyChecking` and the system prompt all match /key|secret|token/
    while leaking nothing. Only the values themselves settle it.
    """
    out = {}
    for server, cfg in bot_module._mcp_config.items():
        for name, value in ((cfg.get("env") or {}) if isinstance(cfg, dict) else {}).items():
            if isinstance(value, str) and len(value) >= 6:
                out[f"{server}.{name}"] = value
    return out


def scan_argv(root: int, secrets: dict[str, str]) -> int:
    leaks = 0
    for pid in descendants(root):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.decode("utf-8", "replace").split("\0") if a]
        if not argv:
            continue
        joined = "\0".join(argv)
        leaked = sorted(name for name, value in secrets.items() if value in joined)
        pattern_args = sum(1 for a in argv if SECRET_RE.search(a))
        print(f"  pid={pid} args={len(argv)} exe={Path(argv[0]).name[:40]} "
              f"value_leaks={len(leaked)} pattern_matches={pattern_args}")
        for name in leaked:
            print(f"    !! LEAKED VALUE of {name}")
        leaks += len(leaked)
    return leaks


async def main() -> int:
    print(f"MCP config keys: {list(bot_module._mcp_config.keys())}")

    registry = ChatRegistry(
        bot=bot_module.bot,
        mcp_config=bot_module._mcp_config,
        system_prompt=bot_module._system_prompt,
        model=MODEL,
        debounce_sec=DEBOUNCE_SEC,
        ask_fn=_rs._ask,
        set_current_chat_fn=set_current_chat,
        get_lazy_block_fn=_reminders.get_lazy_block_for_prompt,
        compact_session_fn=_compact.compact_session,
        activity_store=msg_db(),
        work_dir=WORK_DIR,
        runtime=RUNTIME,
    )
    bot_module.registry = registry
    set_current_chat(PROBE_CHAT)

    secrets = secret_values()
    print(f"tracking {len(secrets)} secret values: {sorted(secrets)}")

    session = registry.get(PROBE_CHAT).session
    await session._ensure_connected()
    print("\n=== argv scan (bot + every CLI descendant) ===")
    hits = scan_argv(os.getpid(), secrets)
    print(f"argv value leaks: {hits}")

    cfg = Path(bot_module.__file__).parent / "storage" / "mcp-external.json"
    print(f"external config: {cfg} mode={oct(cfg.stat().st_mode & 0o777)}")

    hold = int(os.getenv("KESHA_PROBE_HOLD", "0"))
    if hold:
        # Keeps the tree alive so the ticket's own `tr ... < /proc/<pid>/cmdline`
        # command can be run against it from a shell.
        print("HOLD_PIDS " + " ".join(str(p) for p in descendants(os.getpid())), flush=True)
        await asyncio.sleep(hold)
        await session.safe_disconnect()
        return 1 if hits else 0

    prompt = (
        "Do exactly two tool calls and nothing else, then report both raw outputs:\n"
        "1. mcp__kesha__get_bot_status with {}\n"
        "2. mcp__websearch__search with query 'capital of France' and model 'sonar'\n"
    )
    print("\n=== live tool calls ===")
    async for ev in session.send_message(prompt):
        kind = ev.get("type")
        if kind == "tool":
            print(f"  [tool] {ev.get('name')}")
        elif kind == "result":
            print(f"  [result error={ev.get('error')}] {str(ev.get('content'))[:400]}")
        elif kind == "text":
            print(f"  [text] {str(ev.get('content'))[:800]}")
        elif kind == "error":
            print(f"  [error] {str(ev)[:300]}")

    print("\n=== argv scan after tool calls ===")
    hits += scan_argv(os.getpid(), secrets)
    print(f"argv value leaks total: {hits}")

    await session.safe_disconnect()
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
