import asyncio, sys, tempfile, pathlib
sys.path.insert(0, ".")
from claude_session import ClaudeSession
async def main():
    d = pathlib.Path(tempfile.mkdtemp())
    s = ClaudeSession(cwd="/tmp/v38probe", model="claude-haiku-4-5-20251001", session_file=d/"sid")
    s.use_1m = False
    for p in ["Remember the number 42. Reply just: ok", "What number? one word"]:
        print([c async for c in s.send_message(p)][:3])
    print("BEFORE", (await s.get_context_usage()) and (await s.get_context_usage()).get("totalTokens"))
    print("NATIVE", await s.native_compact())
    print("AFTER", (await s.get_context_usage()).get("totalTokens"))
    print([c async for c in s.send_message("What number? one word")][:3])
    await s.safe_disconnect()
asyncio.run(main())
