"""V-45 analysis. BENCH=... python analyze.py -> prints tables (aggregates only)."""
import itertools, json, os, random, statistics as st, sys
from pathlib import Path
B = os.environ.get("BENCH", "/home/kesha/bench-V-45"); sys.path.insert(0, B)
from questions import Q
raw = {f.stem: json.loads(f.read_text()) for f in (Path(B) / "raw").glob("*.json")}
J = json.loads((Path(B) / "judged.json").read_text())
topk = json.loads((Path(B) / "topk.json").read_text()) if (Path(B) / "topk.json").exists() else {}
def per_q(arm, key):
    d = {}
    for r in raw.values():
        if r["arm"] != arm: continue
        v = (J.get(r["run"]) == "CORRECT") if key == "correct" else (r["retrieved"] if key == "retrieved" else r[key])
        d.setdefault(r["qid"], []).append(float(v))
    return {k: sum(v) / len(v) for k, v in d.items()}
def signflip_p(ds):  # one-sided, H0: mean d >= 0 ; p = P(mean <= observed)
    n = len(ds); obs = sum(ds)
    if n > 20:
        rnd = random.Random(1); c = sum(sum(x * rnd.choice((-1, 1)) for x in ds) <= obs + 1e-12 for _ in range(200000)); return c / 200000
    c = sum(sum(s * x for s, x in zip(sg, ds)) <= obs + 1e-12 for sg in itertools.product((1, -1), repeat=n)); return c / 2 ** n
def boot(ds, n=20000):
    rnd = random.Random(2); m = sorted(sum(rnd.choice(ds) for _ in ds) / len(ds) for _ in range(n)); return m[int(.025 * n)], m[int(.975 * n)]
kinds = {"hist-P": lambda q: q["src"] == "msg" and q["kind"] == "P", "notes-P": lambda q: q["src"] == "file" and q["kind"] == "P",
         "pooled-P": lambda q: q["kind"] == "P", "hist-D": lambda q: q["src"] == "msg" and q["kind"] == "D", "notes-D": lambda q: q["src"] == "file" and q["kind"] == "D"}
for key in ("correct", "retrieved"):
    A, Bm = per_q("A", key), per_q("B", key)
    print(f"== metric: {key}  (A = text, B = semantic; per-question mean over repeats)")
    for name, f in kinds.items():
        ids = [q["id"] for q in Q if f(q) and q["id"] in A and q["id"] in Bm]
        if not ids: continue
        ds = [A[i] - Bm[i] for i in ids]
        lo, hi = boot(ds)
        print(f"{name:9s} n={len(ids):2d}  A={sum(A[i] for i in ids)/len(ids):.3f}  B={sum(Bm[i] for i in ids)/len(ids):.3f}  d=A-B={sum(ds)/len(ds):+.3f}  CI95=[{lo:+.3f},{hi:+.3f}]  p(sem better)={signflip_p([-x for x in ds][::1]) if False else signflip_p(ds):.3f}")
print("== cost / behaviour (mean per run)")
for arm in "AB":
    rs = [r for r in raw.values() if r["arm"] == arm]
    print(arm, "runs", len(rs), "rc!=0", sum(r["rc"] != 0 for r in rs), "outside_cwd", sum(bool(r["outside_cwd"]) for r in rs),
          "search", round(st.mean(r["n_search"] for r in rs), 2), "read", round(st.mean(r["n_read"] for r in rs), 2),
          "paid_tok", round(st.mean(r["paid_tokens"] for r in rs)), "sec", round(st.mean(r["seconds"] for r in rs), 1),
          "api$", round(st.mean(r["cost_usd_api_equiv"] or 0 for r in rs), 3))
if topk:
    for name, f in kinds.items():
        ids = [q["id"] for q in Q if f(q)]
        print("pure hybrid", name, {k: round(sum(topk[i][k] for i in ids) / len(ids), 3) for k in ("top5", "top10")})
print("== per-question (correct A/B, retrieved A/B)")
A, Bm, Ar, Br = per_q("A", "correct"), per_q("B", "correct"), per_q("A", "retrieved"), per_q("B", "retrieved")
for q in Q: print(q["id"], q["kind"], A.get(q["id"]), Bm.get(q["id"]), Ar.get(q["id"]), Br.get(q["id"]))
