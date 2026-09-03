import anyio, time, os, sys
sys.path.insert(0, "/mnt/data/Projects/Python/kesha-tg-bot")
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    opts = ClaudeAgentOptions(model="claude-opus-5[1m]", env={**os.environ, "DISABLE_AUTO_COMPACT":"1"})
    c = ClaudeSDKClient(options=opts)
    await c.connect()
    for i in range(6):
        t=time.monotonic()
        try:
            u = await c.get_context_usage()
            print(f"probe{i}: {time.monotonic()-t:.3f}s total={u.get('totalTokens')} max={u.get('maxTokens')} pct={u.get('percentage')}")
        except Exception as e:
            print(f"probe{i}: {time.monotonic()-t:.3f}s ERR {type(e).__name__}: {e}")
    await c.disconnect()

anyio.run(main)
