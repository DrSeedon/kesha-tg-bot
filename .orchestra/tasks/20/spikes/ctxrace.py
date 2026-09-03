import anyio, time, os
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    opts = ClaudeAgentOptions(model="claude-opus-5[1m]", env={**os.environ,"DISABLE_AUTO_COMPACT":"1"})
    c = ClaudeSDKClient(options=opts)
    await c.connect()
    # Consumer A drains receive_messages (mimics send_message's async for)
    await c.query("Count 1 to 30, one per line.")
    async def drain():
        async for m in c.receive_messages():
            if type(m).__name__ == "ResultMessage":
                print("  [drain] result", flush=True); break
    async def probe():
        await anyio.sleep(0.5)
        for i in range(3):
            t=time.monotonic()
            try:
                u=await c.get_context_usage()
                print(f"  [probe{i}] {time.monotonic()-t:.2f}s ok pct={u.get('percentage')}",flush=True)
            except Exception as e:
                print(f"  [probe{i}] {time.monotonic()-t:.2f}s ERR {e}",flush=True)
    async with anyio.create_task_group() as tg:
        tg.start_soon(drain); tg.start_soon(probe)
    await c.disconnect()
anyio.run(main)
