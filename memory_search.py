"""Text search over the chat history (messages.db). No embeddings: the caller picks the words.

Notes (markdown files) are not searched here - the agent greps them with its native tools.
Each call opens its own read-only connection, so a slow regex never touches the bot's write connection.
"""
import re
import sqlite3
from pathlib import Path

MAX_PATTERN = 300
MAX_LIMIT = 30
SNIPPET = 160


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def search_history(path: Path, chat_id: int, pattern: str, role: str | None = None,
                   since: str | None = None, until: str | None = None, limit: int = 10) -> str:
    pattern = (pattern or "").strip()[:MAX_PATTERN]
    if not pattern:
        return "pattern is required"
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    limit = min(max(int(limit or 10), 1), MAX_LIMIT)
    sql, args = "SELECT id, timestamp, role, content FROM messages WHERE chat_id=?", [chat_id]
    if role:
        sql += " AND role=?"
        args.append(role)
    if since:
        sql += " AND timestamp>=?"
        args.append(since)
    if until:
        sql += " AND timestamp<=?"
        args.append(until + "T23:59:59")
    hits = []
    con = _connect(path)
    try:
        for mid, ts, ro, content in con.execute(sql + " ORDER BY id DESC", args):
            m = rx.search(content)
            if not m:
                continue
            a, b = max(0, m.start() - SNIPPET), min(len(content), m.end() + SNIPPET)
            snippet = ("…" if a else "") + content[a:b] + ("…" if b < len(content) else "")
            hits.append(f"[{mid} | {ts} | {ro}] " + snippet.replace("\n", " "))
    finally:
        con.close()
    if not hits:
        return "0 matches"
    return f"{len(hits)} matches, showing {min(limit, len(hits))} newest\n" + "\n".join(hits[:limit])


def read_messages(path: Path, chat_id: int, message_id: int, before: int = 2, after: int = 2) -> str:
    before, after = min(max(int(before), 0), 10), min(max(int(after), 0), 10)
    con = _connect(path)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]
        if message_id not in ids:
            return "no such message"
        k = ids.index(message_id)
        sel = ids[max(0, k - before): k + after + 1]
        rows = con.execute(
            f"SELECT id, timestamp, role, content FROM messages WHERE id IN ({','.join('?' * len(sel))}) ORDER BY id", sel
        ).fetchall()
    finally:
        con.close()
    return "\n\n".join(f"[{i} | {ts} | {ro}] {c}" for i, ts, ro, c in rows)
