"""Arm B tool: today's search_memory (rag.RagMemory.search, hybrid bge-m3 + FTS5 + RRF) over a COPY of the prod index.
Same name, description, schema and output format as kesha_tools.search_memory. Env: CHAT_ID, BENCH (dir with vec.db, messages.db), RAGSRC (dir with rag.py)."""
import os, sys
sys.path.insert(0, os.environ["RAGSRC"])
from pathlib import Path
from mcp.server.fastmcp import FastMCP
import rag

B = Path(os.environ["BENCH"]); CHAT = int(os.environ["CHAT_ID"])
mem = rag.RagMemory(path=B / "vec.db", msg_db=B / "messages.db", readonly=True)
mcp = FastMCP("mem")

@mcp.tool(name="search_memory", description=(
      "Semantic search across the ENTIRE knowledge base: dialog history AND the user's file notes "
      "(cog-second-brain — diaries, daily dumps, projects). Survives context compaction/reset. "
      "Use when the user references the past ('помнишь', 'что я говорил про…', 'мы обсуждали') or "
      "asks about their notes/knowledge. Do NOT call on every message — only when memory is needed. "
      "Results are tagged: [file: path] for notes, [timestamp | role] for dialog. "
      "query: what to find (natural language). role: optional filter 'user'/'assistant' "
      "(filters dialog messages by role; files are always searched). limit: max results (default 5)."))
def search_memory(query: str, limit: int = 5, role: str = "") -> str:
    query = (query or "").strip()
    if not query:
        return "query is required"
    limit = min(max(int(limit or 5), 1), 20)
    rows = mem.search(CHAT, query, limit, role or None)
    if not rows:
        return "No matches in history"
    def _fmt(r):
        if r.get("source") == "file":
            return f"[file: {r['path']}] {r['content']}"
        return f"[{r['timestamp']} | {r['role']}] {r['content']}"
    return "\n".join(_fmt(r) for r in rows)

mcp.run()
