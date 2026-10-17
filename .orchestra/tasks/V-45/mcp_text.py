"""Arm A tool (prototype of the stage-2 design): text search over the chat history, no embeddings.
Env: CHAT_ID, BENCH (dir with messages.db)."""
import os, re, sqlite3
from mcp.server.fastmcp import FastMCP

CHAT = int(os.environ["CHAT_ID"])
db = sqlite3.connect(f"file:{os.environ['BENCH']}/messages.db?mode=ro", uri=True)
mcp = FastMCP("mem")

@mcp.tool(name="search_memory", description=(
    "Text search (like grep) over the chat history of THIS chat: case-insensitive regex over message text. "
    "It matches characters, not meaning — you choose the words the original message likely used: word stems "
    "and alternations, e.g. `фиолетов|сиренев`, identifiers, numbers, names. Results: `[id | time | role] snippet`, "
    "newest first, with the total match count. Too many hits -> add a second word or since/until; none -> try "
    "another word form or synonym. Notes (markdown files) are NOT searched here — use Grep/Glob/Read on the "
    "working directory. pattern: regex. role: optional 'user'/'assistant'. since/until: YYYY-MM-DD. limit: default 10, max 30."))
def search_memory(pattern: str, role: str = "", since: str = "", until: str = "", limit: int = 10) -> str:
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    limit = min(max(int(limit or 10), 1), 30)
    sql = "select id,timestamp,role,content from messages where chat_id=?"
    args = [CHAT]
    if role: sql += " and role=?"; args.append(role)
    if since: sql += " and timestamp>=?"; args.append(since)
    if until: sql += " and timestamp<=?"; args.append(until + "T23:59:59")
    hits = []
    for i, ts, ro, c in db.execute(sql + " order by id desc", args):
        m = rx.search(c)
        if m:
            a, b = max(0, m.start() - 160), min(len(c), m.end() + 160)
            hits.append(f"[{i} | {ts} | {ro}] {'…' if a else ''}{c[a:b]}{'…' if b < len(c) else ''}".replace("\n", " "))
    if not hits:
        return "0 matches"
    return f"{len(hits)} matches, showing {min(limit, len(hits))} newest\n" + "\n".join(hits[:limit])

@mcp.tool(name="read_messages", description=(
    "Read a message in full by id plus `before`/`after` neighbouring messages of the same chat (default 2 and 2)."))
def read_messages(id: int, before: int = 2, after: int = 2) -> str:
    before, after = min(max(before, 0), 10), min(max(after, 0), 10)
    ids = [r[0] for r in db.execute("select id from messages where chat_id=? order by id", (CHAT,))]
    if id not in ids:
        return "no such message"
    k = ids.index(id)
    sel = ids[max(0, k - before): k + after + 1]
    rows = db.execute(f"select id,timestamp,role,content from messages where id in ({','.join('?'*len(sel))}) order by id", sel)
    return "\n\n".join(f"[{i} | {ts} | {ro}] {c}" for i, ts, ro, c in rows)

mcp.run()
