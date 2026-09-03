import anyio, asyncio, time, os, subprocess
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    opts = ClaudeAgentOptions(model="claude-opus-5[1m]", env={**os.environ,"DISABLE_AUTO_COMPACT":"1"})
    c = ClaudeSDKClient(options=opts)
    await c.connect()
    q = c._query
    pid = c._transport._process.pid
    print("baseline pending:", len(q.pending_control_responses), len(q.pending_control_results), flush=True)
    subprocess.run(["kill","-STOP",str(pid)])
    for i in range(3):
        t=time.monotonic()
        try:
            await asyncio.wait_for(c.get_context_usage(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            print(f"outer-timeout{i}: {time.monotonic()-t:.1f}s  pending_resp={len(q.pending_control_responses)} pending_res={len(q.pending_control_results)}", flush=True)
        except Exception as e:
            print(f"other{i}: {type(e).__name__} {e}", flush=True)
    subprocess.run(["kill","-CONT",str(pid)])
    t=time.monotonic()
    try:
        u = await asyncio.wait_for(c.get_context_usage(), timeout=15)
        print(f"recovery: {time.monotonic()-t:.1f}s ok pct={u.get('percentage')} pending_resp={len(q.pending_control_responses)}", flush=True)
    except Exception as e:
        print(f"recovery FAILED: {type(e).__name__} {e} pending_resp={len(q.pending_control_responses)}", flush=True)
    await c.disconnect()
asyncio.run(main())
