"""Blind judge (Luna) for V-45. Run from /home/kesha/orchestra with its venv:
  BENCH=/home/kesha/bench-V-45 .venv/bin/python <this file>   -> $BENCH/judged.json {run: verdict}"""
import asyncio, json, os, re, sys, tempfile
from pathlib import Path
ROOT = Path("/home/kesha/orchestra"); sys.path.insert(0, str(ROOT))
import scripts.wf_adapters as wa
from app.models import resolve_model
import sqlite3
B = os.environ.get("BENCH", "/home/kesha/bench-V-45"); sys.path.insert(0, B)
from questions import Q
QD = {q["id"]: q for q in Q}
db = sqlite3.connect(f"{B}/messages.db")
def excerpt(q):
    t = db.execute("select content from messages where id=?", (q["target"],)).fetchone()[0] if q["src"] == "msg" \
        else open(f"{B}/corpus/{q['target']}", encoding="utf-8", errors="ignore").read()
    m = re.search(q["gold"], t)
    return t[max(0, m.start() - 600): m.end() + 600]
PROMPT = """You grade an assistant's answer to a question about a user's private memory. You see the question, a REFERENCE excerpt from the source that holds the true answer, and the assistant's ANSWER. The assistant had to find the fact itself.

QUESTION: {q}

REFERENCE (true source text):
<<<
{ref}
>>>

ANSWER (assistant's final reply):
<<<
{ans}
>>>

Verdict CORRECT = the answer states the fact the question asks for and it agrees with the reference (extra detail is fine if it does not contradict it). INCORRECT = it states a different or conflicting fact, only a vague/partial fact that does not answer the question, or says it could not find it.
Reply with one line of JSON only: {{"verdict": "CORRECT"}} or {{"verdict": "INCORRECT"}}."""
async def main():
    out_p = Path(B) / "judged.json"; done = json.loads(out_p.read_text()) if out_p.exists() else {}
    sem = asyncio.Semaphore(4); model = resolve_model("luna")
    async def one(f):
        r = json.loads(f.read_text())
        if r["run"] in done: return
        txt = (r["final"] or "").strip()
        if r["rc"] != 0 or not txt: done[r["run"]] = "INCORRECT"; return
        async with sem:
            res = await wa.run_adapter(PROMPT.format(q=QD[r["qid"]]["q"], ref=excerpt(QD[r["qid"]]), ans=txt), model=model,
                                       cwd=Path(tempfile.mkdtemp()), timeout=300, tools="read", network=False, mcp=False, billing_mode="subscription")
        m = re.search(r"(INCORRECT|CORRECT)", res.text or "")
        done[r["run"]] = m.group(1) if m else "ERROR"
        print(r["run"], done[r["run"]], flush=True)
    await asyncio.gather(*[one(f) for f in sorted((Path(B) / "raw").glob("*.json"))])
    out_p.write_text(json.dumps(done, indent=1))
asyncio.run(main())
