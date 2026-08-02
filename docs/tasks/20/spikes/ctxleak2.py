import asyncio, time, os, subprocess
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def probe(c, timeout):
    """Bounded probe that does NOT leak: run in a shielded task we can reap later."""
    task = asyncio.create_task(c.get_context_usage())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        # let the SDK's own fail_after(60) clean up its pending entry
        task.add_done_callback(lambda t: t.exception())
        raise

async def main():
    opts = ClaudeAgentOptions(model="claude-opus-5[1m]", env={**os.environ,"DISABLE_AUTO_COMPACT":"1"})
    c = ClaudeSDKClient(options=opts); await c.connect()
    q = c._query; pid = c._transport._process.pid
    subprocess.run(["kill","-STOP",str(pid)])
    for i in range(3):
        try: await probe(c, 5)
        except Exception as e: print(f"t{i}: pending={len(q.pending_control_responses)}", flush=True)
    subprocess.run(["kill","-CONT",str(pid)])
    print("after CONT, waiting for SDK's own 60s cleanup to reap orphans...", flush=True)
    await asyncio.sleep(62)
    print("pending after SDK cleanup:", len(q.pending_control_responses), flush=True)
    await c.disconnect()
asyncio.run(main())
