"""Live probe: drive one real Codex turn and record every notification.

Ground truth for the T4 event mapping. Run:
    uv run python docs/tasks/16/spikes/turn_probe.py
"""

import asyncio
import json
import os
import sys

CODEX = os.path.expanduser("~/.npm-global/bin/codex")


class Probe:
    def __init__(self):
        self.proc = None
        self.pending = {}
        self.seq = 0
        self.events = []

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            CODEX, "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            cwd="/tmp",
        )
        asyncio.create_task(self._read())
        asyncio.create_task(self._stderr())

    async def _stderr(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            sys.stderr.write("[stderr] " + line.decode(errors="replace"))

    async def _read(self):
        while True:
            try:
                line = await self.proc.stdout.readline()
            except Exception as e:
                print(f"[read error] {e}")
                break
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            else:
                self.events.append(msg)
                method = msg.get("method", "?")
                params = msg.get("params", {})
                short = json.dumps(params, ensure_ascii=False)
                if len(short) > 300:
                    short = short[:300] + "..."
                print(f"  EVENT {method}: {short}")

    async def request(self, method, params):
        self.seq += 1
        rid = self.seq
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        self.proc.stdin.write(payload.encode() + b"\n")
        await self.proc.stdin.drain()
        msg = await asyncio.wait_for(fut, timeout=180)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    async def notify(self, method, params):
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self.proc.stdin.write(payload.encode() + b"\n")
        await self.proc.stdin.drain()


async def main():
    p = Probe()
    await p.start()

    print("=== initialize ===")
    r = await p.request("initialize", {
        "clientInfo": {"name": "kesha-probe", "title": "Kesha", "version": "1"}
    })
    print(json.dumps(r, ensure_ascii=False)[:400])
    await p.notify("initialized", {})

    print("\n=== account/rateLimits/read ===")
    try:
        rl = await p.request("account/rateLimits/read", {})
        print(json.dumps(rl, ensure_ascii=False)[:800])
    except Exception as e:
        print(f"rateLimits failed: {e}")

    print("\n=== thread/start ===")
    r = await p.request("thread/start", {
        "cwd": "/tmp",
        "model": "gpt-5.6-sol",
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "developerInstructions": "You are Kesha, a terse assistant. Answer in Russian.",
    })
    print(json.dumps(r, ensure_ascii=False)[:400])
    thread_id = (r.get("thread") or {}).get("id")
    print(f"thread_id={thread_id}")

    print("\n=== turn/start (live generation) ===")
    r = await p.request("turn/start", {
        "threadId": thread_id,
        "input": [{"type": "text", "text": "Скажи одним предложением, что ты работаешь. Без инструментов."}],
        "model": "gpt-5.6-sol",
    })
    print(f"turn/start result: {json.dumps(r, ensure_ascii=False)[:300]}")

    # wait for turn/completed
    for _ in range(180):
        await asyncio.sleep(1)
        if any(e.get("method") == "turn/completed" for e in p.events):
            break

    print("\n=== distinct methods seen ===")
    methods = {}
    for e in p.events:
        methods[e.get("method")] = methods.get(e.get("method"), 0) + 1
    for m, c in sorted(methods.items()):
        print(f"  {m}: {c}")

    out = "/tmp/turn_probe_events.jsonl"
    with open(out, "w") as f:
        for e in p.events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(p.events)} events -> {out}")

    p.proc.terminate()


asyncio.run(main())
