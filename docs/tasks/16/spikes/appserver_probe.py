import json,subprocess,threading,time,sys
p=subprocess.Popen(["codex","app-server","--stdio"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
def send(o): p.stdin.write(json.dumps(o)+"\n"); p.stdin.flush()
send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"spike","title":"s","version":"0.0.1"}}})
send({"jsonrpc":"2.0","method":"initialized","params":{}})
send({"jsonrpc":"2.0","id":2,"method":"thread/start","params":{"cwd":"/tmp/spike16","sandbox":"read-only"}})
send({"jsonrpc":"2.0","id":3,"method":"account/rateLimits/read","params":{}})
out=[]
def rd():
    for l in p.stdout:
        out.append(l.strip())
t=threading.Thread(target=rd,daemon=True); t.start()
time.sleep(12)
tid=None
for l in out:
    try: d=json.loads(l)
    except: continue
    if d.get("id")==2 and "result" in d: tid=(d["result"].get("thread") or {}).get("id") or d["result"].get("threadId")
print("THREAD_ID:",tid)
if tid:
    send({"jsonrpc":"2.0","id":4,"method":"turn/start","params":{"threadId":tid,"input":[{"type":"text","text":"say PONG"}]}})
    time.sleep(15)
for l in out[:40]: print(l[:400])
p.kill()
