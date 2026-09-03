"""What does thread/compact/start actually emit? Are we waiting for the wrong event?"""
import asyncio, json, os, sys
CODEX=os.path.expanduser("~/.npm-global/bin/codex")

async def main():
    proc=await asyncio.create_subprocess_exec(CODEX,"app-server","--stdio",
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,limit=16*1024*1024,cwd="/tmp",
        env={**os.environ})
    pending={}; seq=0; events=[]
    async def read():
        while True:
            line=await proc.stdout.readline()
            if not line: break
            try: m=json.loads(line)
            except Exception: continue
            if "id" in m and ("result" in m or "error" in m):
                f=pending.pop(m["id"],None)
                if f and not f.done(): f.set_result(m)
            else:
                events.append(m)
    asyncio.create_task(read())
    async def req(method,params,timeout=120):
        nonlocal seq
        seq+=1; rid=seq
        f=asyncio.get_running_loop().create_future(); pending[rid]=f
        proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params}).encode()+b"\n")
        await proc.stdin.drain()
        m=await asyncio.wait_for(f,timeout=timeout)
        if "error" in m: raise RuntimeError(m["error"])
        return m.get("result") or {}

    await req("initialize",{"clientInfo":{"name":"p","title":"p","version":"1"}})
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":"initialized","params":{}}).encode()+b"\n")
    await proc.stdin.drain()
    r=await req("thread/start",{"cwd":"/tmp","model":"gpt-5.6-sol","approvalPolicy":"never","sandbox":"read-only"})
    tid=(r.get("thread") or {}).get("id")

    for i in range(3):
        await req("turn/start",{"threadId":tid,"input":[{"type":"text","text":f"Перечисли {10+i} городов, только список."}],"model":"gpt-5.6-sol"})
        for _ in range(120):
            await asyncio.sleep(1)
            if any(e.get("method")=="turn/completed" for e in events[-6:]): break
    usage=[e for e in events if e.get("method")=="thread/tokenUsage/updated"]
    print("last usage before compact:", json.dumps(usage[-1]["params"]["tokenUsage"]["last"],ensure_ascii=False) if usage else None)

    events.clear()
    print("\n--- thread/compact/start ---")
    res=await req("thread/compact/start",{"threadId":tid})
    print("immediate result:", json.dumps(res,ensure_ascii=False)[:200])
    await asyncio.sleep(25)
    print("\nevents after compact request:")
    for e in events:
        m=e.get("method")
        p=json.dumps(e.get("params",{}),ensure_ascii=False)
        print(f"  {m}: {p[:220]}")
    proc.terminate()

asyncio.run(main())
