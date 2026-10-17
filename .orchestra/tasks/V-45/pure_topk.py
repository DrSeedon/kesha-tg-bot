"""Pure hybrid top-k retrieval (arm B without an agent): the query is the question text. Run in the bot venv:
  BENCH=... RAGSRC=<worktree> /opt/kesha-bot/.venv/bin/python pure_topk.py  -> $BENCH/topk.json"""
import json, os, re, sys
B = os.environ["BENCH"]; sys.path.insert(0, os.environ["RAGSRC"]); sys.path.insert(0, B)
from pathlib import Path
import rag
from questions import Q
mem = rag.RagMemory(path=Path(B) / "vec.db", msg_db=Path(B) / "messages.db", readonly=True)
out = {}
for q in Q:
    rows = mem.search(q["chat"], q["q"], 10, None); g = re.compile(q["gold"])
    hit = [bool(g.search(r["content"])) for r in rows]
    out[q["id"]] = dict(top5=int(any(hit[:5])), top10=int(any(hit)))
json.dump(out, open(f"{B}/topk.json", "w"), indent=1)
