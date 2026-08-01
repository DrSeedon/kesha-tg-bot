"""Probe: does Kesha's Codex thread really see ONLY its own MCP servers?

The user's global ~/.codex/config.toml defines serena, kwin, orchestra,
codex_apps and openaiDeveloperDocs. A bare app-server starts all of them
(recorded in turn_probe_events.jsonl). This measures whether the override
actually suppresses them.

Run:  uv run python docs/tasks/16/spikes/mcp_isolation_probe.py
"""

import asyncio
import json
import os
import sys

CODEX = os.path.expanduser("~/.npm-global/bin/codex")


async def collect(extra_args, label):
    proc = await asyncio.create_subprocess_exec(
        CODEX, "app-server", "--stdio", *extra_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=16 * 1024 * 1024,
        cwd="/tmp",
    )
    seen = set()
    pending = {}
    seq = 0
    done = asyncio.Event()

    async def read():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") == "mcpServer/startupStatus/updated":
                seen.add(msg["params"].get("name"))

    asyncio.create_task(read())

    async def request(method, params):
        nonlocal seq
        seq += 1
        rid = seq
        fut = asyncio.get_running_loop().create_future()
        pending[rid] = fut
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        ).encode() + b"\n")
        await proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=60)

    await request("initialize", {"clientInfo": {"name": "probe", "title": "p", "version": "1"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}).encode() + b"\n")
    await proc.stdin.drain()

    r = await request("thread/start", {
        "cwd": "/tmp",
        "model": "gpt-5.6-sol",
        "approvalPolicy": "never",
        "sandbox": "read-only",
    })
    thread_id = (r.get("result", {}).get("thread") or {}).get("id")

    # Servers start lazily; give them a beat to announce themselves.
    await asyncio.sleep(6)

    # Authoritative list, not just the startup chatter.
    listed = set()
    try:
        r = await request("mcpServerStatus/list", {"threadId": thread_id})
        payload = r.get("result") or {}
        for entry in (payload.get("servers") or payload.get("mcpServers") or []):
            if isinstance(entry, dict):
                listed.add(entry.get("name"))
            else:
                listed.add(str(entry))
    except Exception as e:
        listed = {f"<list failed: {e}>"}

    print(f"\n--- {label} ---")
    print(f"  startupStatus announced: {sorted(x for x in seen if x) or '(none)'}")
    print(f"  mcpServerStatus/list   : {sorted(x for x in listed if x) or '(none)'}")

    proc.terminate()
    return seen, listed


async def main():
    baseline, baseline_list = await collect([], "BASELINE (bare app-server, inherits global config)")

    override = ["-c", "mcp_servers={}"]
    pinned, pinned_list = await collect(override, "KESHA (-c mcp_servers={})")

    print("\n=== VERDICT ===")
    leaked = {x for x in (pinned | pinned_list) if x} - {"kesha"}
    print(f"  inherited in baseline : {sorted(x for x in (baseline | baseline_list) if x)}")
    print(f"  leaked into Kesha     : {sorted(leaked) or '(none)'}")
    print("  RESULT:", "ISOLATED ✅" if not leaked else "LEAK ❌")


asyncio.run(main())

# MEASURED RESULT (codex-cli 0.145.0, 2026-08-01):
#
#   A. bare app-server              -> serena, kwin, orchestra, openaiDeveloperDocs, codex_apps
#   B. -c mcp_servers={}            -> serena, kwin, orchestra, openaiDeveloperDocs, codex_apps  (NO EFFECT)
#   C. CODEX_HOME=<private>         -> codex_apps only
#   D. CODEX_HOME + --disable apps  -> (none)
#   E. D + kesha bridge             -> kesha only          <- what CodexSession uses
#
# `-c` MERGES into the user's config, it does not replace it. Only a private
# CODEX_HOME actually drops the global mcp_servers table. `codex_apps` is a
# built-in behind the `apps` feature flag, not a config entry.
