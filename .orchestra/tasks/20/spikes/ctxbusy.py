import anyio, time, os, sys
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    opts = ClaudeAgentOptions(model="claude-opus-5[1m]", env={**os.environ, "DISABLE_AUTO_COMPACT":"1"})
    c = ClaudeSDKClient(options=opts)
    await c.connect()
    await c.query("Count slowly from 1 to 40, one number per line, no other text.")
    async def poll():
        for i in range(8):
            t=time.monotonic()
            try:
                u = await c.get_context_usage()
                print(f"  busy-probe{i}: {time.monotonic()-t:.3f}s total={u.get('totalTokens')} pct={u.get('percentage')}", flush=True)
            except Exception as e:
                print(f"  busy-probe{i}: {time.monotonic()-t:.3f}s ERR {e}", flush=True)
            await anyio.sleep(1.5)
    async with anyio.create_task_group() as tg:
        tg.start_soon(poll)
        async for m in c.receive_response():
            pass
        print("turn done", flush=True)
    await c.disconnect()

anyio.run(main)
