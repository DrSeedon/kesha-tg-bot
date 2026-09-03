import asyncio, os, sys, time, subprocess
sys.path.insert(0,"/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5")
os.chdir("/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5")
import claude_session as cs

async def main():
    s = cs.ClaudeSession(cwd="/tmp", model="claude-opus-5", session_file=__import__("pathlib").Path("/tmp/e2e-sess"))
    await s._ensure_connected()
    print("healthy reserve:", {k:v for k,v in (await s.check_context_reserve("hi")).items() if k!="usage"}, flush=True)
    pid = s._client._transport._process.pid
    sid_before = s.session_id
    subprocess.run(["kill","-STOP",str(pid)])
    t=time.monotonic()
    out = await s.check_context_reserve("hi")
    print(f"wedged reserve: {time.monotonic()-t:.1f}s -> ok={out['ok']} reason={out['reason']}", flush=True)
    print(f"client dropped={s._client is None}  session_id preserved={s.session_id == sid_before!r} ({s.session_id})", flush=True)
    subprocess.run(["kill","-CONT",str(pid)])
    q = None
    t=time.monotonic()
    out2 = await s.check_context_reserve("hi")
    print(f"after reconnect: {time.monotonic()-t:.1f}s -> ok={out2['ok']} reason={out2['reason']}", flush=True)
    try: await s._safe_disconnect(s._client)
    except Exception: pass
asyncio.run(main())
