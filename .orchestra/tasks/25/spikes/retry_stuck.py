import asyncio, os, sys, subprocess, pathlib, time
sys.path.insert(0,"/mnt/data/Projects/Python/kesha-tg-bot")
os.chdir("/mnt/data/Projects/Python/kesha-tg-bot")
import claude_session as cs

async def main():
    f=pathlib.Path("/tmp/e2e-25b"); f.unlink(missing_ok=True)
    s=cs.ClaudeSession(cwd="/tmp", model="claude-opus-5", session_file=f)
    await s._ensure_connected()
    pid=s._client._transport._process.pid
    subprocess.run(["kill","-STOP",str(pid)])
    r1=await s.check_context_reserve("hi")
    print(f"attempt1: ok={r1['ok']} reason={r1['reason']}")
    # STILL wedged: does the retry spawn a NEW process, or hit the same frozen one?
    t=time.monotonic()
    r2=await s.check_context_reserve("hi")
    newpid = s._client._transport._process.pid if s._client else None
    print(f"retry-while-old-still-STOPPED: {time.monotonic()-t:.1f}s ok={r2['ok']} reason={r2['reason']} newpid={newpid} oldpid={pid} same={newpid==pid}")
    subprocess.run(["kill","-CONT",str(pid)])
    subprocess.run(["kill","-KILL",str(pid)])
asyncio.run(main())
