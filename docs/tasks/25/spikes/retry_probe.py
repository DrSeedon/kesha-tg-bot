import asyncio, os, sys, subprocess, pathlib, time
sys.path.insert(0,"/mnt/data/Projects/Python/kesha-tg-bot")
os.chdir("/mnt/data/Projects/Python/kesha-tg-bot")
import claude_session as cs

async def main():
    f=pathlib.Path("/tmp/e2e-25"); f.unlink(missing_ok=True)
    s=cs.ClaudeSession(cwd="/tmp", model="claude-opus-5", session_file=f)
    await s._ensure_connected()
    sid0=s.session_id
    pid=s._client._transport._process.pid
    subprocess.run(["kill","-STOP",str(pid)])
    t=time.monotonic()
    r1=await s.check_context_reserve("hi")
    print(f"attempt1 (wedged): {time.monotonic()-t:.1f}s ok={r1['ok']} reason={r1['reason']} client={s._client}")
    subprocess.run(["kill","-CONT",str(pid)])
    # immediate retry, no sleep — does reconnect+reserve succeed right away?
    for i in (1,2):
        t=time.monotonic()
        r=await s.check_context_reserve("hi")
        print(f"retry{i}: {time.monotonic()-t:.1f}s ok={r['ok']} reason={r['reason']} sid_preserved={s.session_id==sid0}")
        if r['ok']: break
    try: await s._safe_disconnect(s._pending_disconnect or s._client)
    except Exception: pass
asyncio.run(main())
