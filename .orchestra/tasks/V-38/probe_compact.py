import asyncio, os, sys, json
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
async def main():
    opts = ClaudeAgentOptions(model="claude-haiku-4-5-20251001", cwd="/tmp/v38probe", permission_mode="bypassPermissions",
        env={"DISABLE_AUTO_COMPACT":"1"}, extra_args={"strict-mcp-config":None}, max_turns=3)
    async with ClaudeSDKClient(opts) as c:
        for prompt in ["Remember the number 42. Reply just: ok", "What number? one word", "/compact"]:
            print("=== QUERY", prompt, flush=True)
            await c.query(prompt)
            async for m in c.receive_response():
                print(type(m).__name__, repr(m)[:700], flush=True)
asyncio.run(main())
