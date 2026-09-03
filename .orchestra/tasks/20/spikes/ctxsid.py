import asyncio, os, sys, subprocess, pathlib
sys.path.insert(0,"/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5")
os.chdir("/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5")
import claude_session as cs

async def main():
    f = pathlib.Path("/tmp/e2e-sid"); f.unlink(missing_ok=True)
    s = cs.ClaudeSession(cwd="/tmp", model="claude-opus-5", session_file=f)
    # real turn -> real session_id persisted
    async for _ in s.send_message("say OK"): pass
    sid = s.session_id
    print("real session_id:", sid, "| file:", f.read_text().strip() if f.exists() else None, flush=True)
    pid = s._client._transport._process.pid
    subprocess.run(["kill","-STOP",str(pid)])
    out = await s.check_context_reserve("hi")
    print(f"wedged -> ok={out['ok']} reason={out['reason']}", flush=True)
    print(f"client dropped={s._client is None}  sid preserved={s.session_id == sid}  sid={s.session_id}", flush=True)
    print("file still:", f.read_text().strip() if f.exists() else None, flush=True)
    subprocess.run(["kill","-CONT",str(pid)])
    try: await s._safe_disconnect(s._pending_disconnect or s._client)
    except Exception: pass
asyncio.run(main())
