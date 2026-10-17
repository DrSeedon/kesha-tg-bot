"""V-45 target sampler (seed 45). Writes candidates OUTSIDE the repo (private text).
Usage: python sample.py  -> /home/kesha/bench-V-45/candidates_{msgs,files}.txt"""
import os, random, re, sqlite3
B = "/home/kesha/bench-V-45"
rnd = random.Random(45)
db = sqlite3.connect(f"{B}/messages.db")
rows = db.execute("select id,chat_id,role,timestamp,content from messages where length(content) between 150 and 900 "
                  "and content not like '[%voice%' and content not like '/%' and content not like '%⏰%'").fetchall()
rnd.shuffle(rows)
with open(f"{B}/candidates_msgs.txt", "w") as f:
    for r in rows[:70]:
        f.write(f"=== id={r[0]} chat={r[1]} role={r[2]} ts={r[3]}\n{r[4]}\n\n")
paths = []
for d, _, fs in os.walk(f"{B}/corpus"):
    for x in fs:
        p = os.path.relpath(os.path.join(d, x), f"{B}/corpus")
        if p.startswith(".orchestra/") or p.startswith("06-templates"): continue
        if 800 <= os.path.getsize(os.path.join(B, "corpus", p)) <= 40000: paths.append(p)
paths.sort(); rnd.shuffle(paths)
with open(f"{B}/candidates_files.txt", "w") as f:
    for p in paths[:70]:
        t = open(f"{B}/corpus/{p}", encoding="utf-8", errors="ignore").read()
        secs = [s for s in re.split(r"\n(?=#{1,3} )", t) if 250 <= len(s) <= 1800] or [t[:1200]]
        f.write(f"=== path={p}\n{rnd.choice(secs)[:1000]}\n\n")
print(len(rows), len(paths))
