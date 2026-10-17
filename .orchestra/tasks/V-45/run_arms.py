"""V-45 agent arms. Usage: python run_arms.py <A|B> <rep> [--only M01,F02] [--model sonnet]
Raw results: $BENCH/raw/<arm>-<qid>-r<rep>.json (existing files are skipped). cwd of the agent = $BENCH/corpus."""
import json, os, subprocess, sys, time, tempfile, re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent)); import prompts
BENCH = os.environ.get("BENCH", "/home/kesha/bench-V-45"); sys.path.insert(0, BENCH)
from questions import Q
REF = json.load(open(f"{BENCH}/reference.json"))
PY = "/opt/kesha-bot/.venv/bin/python"; HERE = Path(__file__).parent
RAGSRC = os.environ.get("RAGSRC", str(HERE.parents[2]))
CORPUS = f"{BENCH}/corpus"; RAW = Path(BENCH) / "raw"; RAW.mkdir(exist_ok=True)
arm, rep = sys.argv[1], sys.argv[2]
only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "sonnet"
LAYOUT = prompts.layout_block(CORPUS)

def run(q):
    rid = f"{arm}-{q['id']}-r{rep}"
    out = RAW / f"{rid}.json"
    if out.exists() or (only and q["id"] not in only): return
    env = {"CHAT_ID": str(q["chat"]), "BENCH": BENCH, "RAGSRC": RAGSRC}
    if arm == "B":
        mem = prompts.MEM_B; srv = ["mcp_sem.py"]; tools = "mcp__mem__search_memory"
    else:
        mem = prompts.MEM_A.format(layout=LAYOUT, hist=prompts.hist_block(BENCH, q["chat"])); srv = ["mcp_text.py"]
        tools = "mcp__mem__search_memory mcp__mem__read_messages"
    cfg = {"mcpServers": {"mem": {"command": PY, "args": [str(HERE / srv[0])], "env": env}}}
    cf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(cfg, cf); cf.close()
    cmd = ["claude", "-p", q["q"], "--model", model, "--tools", "Read,Grep,Glob", "--allowedTools", f"Read Grep Glob {tools}",
           "--mcp-config", cf.name, "--strict-mcp-config", "--setting-sources", "", "--max-turns", "16",
           "--system-prompt", prompts.COMMON.format(memory=mem), "--output-format", "stream-json", "--verbose"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=CORPUS, capture_output=True, text=True, timeout=900)
        so, rc, se = p.stdout, p.returncode, p.stderr[-300:]
    except subprocess.TimeoutExpired as e:
        so, rc, se = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), -9, "timeout"
    os.unlink(cf.name)
    calls = []; results = []; final = ""; usage = {}; cost = None
    for l in so.splitlines():
        try: r = json.loads(l)
        except Exception: continue
        if r.get("type") == "assistant":
            for b in r["message"].get("content", []):
                if b.get("type") == "tool_use": calls.append(dict(name=b["name"], input=b["input"]))
        elif r.get("type") == "user":
            c = r["message"].get("content")
            if isinstance(c, list):
                for b in c:
                    if b.get("type") == "tool_result":
                        t = b.get("content"); t = "".join(x.get("text", "") for x in t) if isinstance(t, list) else str(t)
                        results.append(t)
        elif r.get("type") == "result":
            final = r.get("result", ""); usage = r.get("usage", {}); cost = r.get("total_cost_usd")
    gold = re.compile([x for x in Q if x["id"] == q["id"]][0]["gold"])
    outside = [c for c in calls if any(isinstance(v, str) and v.startswith("/") and not v.startswith(CORPUS) for k, v in c["input"].items() if k in ("path", "file_path"))]
    row = dict(run=rid, qid=q["id"], arm=arm, rep=rep, model=model, rc=rc, err=se, seconds=round(time.time() - t0, 1),
               final=final, calls=calls, n_search=sum(c["name"] in ("Grep", "Glob") or c["name"].endswith("search_memory") for c in calls),
               n_read=sum(c["name"] in ("Read",) or c["name"].endswith("read_messages") for c in calls),
               retrieved=any(gold.search(t) for t in results), outside_cwd=outside,
               paid_tokens=usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("output_tokens", 0),
               usage=usage, cost_usd_api_equiv=cost)
    out.write_text(json.dumps(row, ensure_ascii=False, indent=1))
    print(rid, rc, row["n_search"], row["n_read"], row["retrieved"], row["paid_tokens"], flush=True)

qs = [q for q in Q]
with ThreadPoolExecutor(int(os.environ.get("CONC", "3"))) as ex: list(ex.map(run, qs))
