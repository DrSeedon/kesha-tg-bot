import anyio, time, os, subprocess
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    opts = ClaudeAgentOptions(model="claude-opus-5[1m]", env={**os.environ,"DISABLE_AUTO_COMPACT":"1"})
    c = ClaudeSDKClient(options=opts)
    await c.connect()
    u = await c.get_context_usage(); print("before kill:", u.get("percentage"), flush=True)
    # find and SIGSTOP the CLI child (simulate unresponsive, not dead)
    pid = c._transport._process.pid
    print("cli pid", pid, flush=True)
    subprocess.run(["kill","-STOP",str(pid)])
    t=time.monotonic()
    try:
        await c.get_context_usage(); print("unexpected ok")
    except Exception as e:
        print(f"STOPPED-cli: {time.monotonic()-t:.1f}s ERR {e}", flush=True)
    subprocess.run(["kill","-CONT",str(pid)])
    t=time.monotonic()
    try:
        u=await c.get_context_usage(); print(f"after CONT: {time.monotonic()-t:.1f}s ok pct={u.get('percentage')}", flush=True)
    except Exception as e:
        print(f"after CONT: {time.monotonic()-t:.1f}s ERR {e}", flush=True)
    await c.disconnect()
anyio.run(main)
