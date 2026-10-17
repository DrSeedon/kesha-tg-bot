"""Frozen prompts of the V-45 agent arms. B = today's memory section (system_prompt.txt) verbatim; A = the proposed text-search section."""
import collections, os, re, sqlite3

COMMON = """You are Kesha, the personal assistant bot of a family, working over Telegram. Your notes - a personal knowledge base of markdown files - are in the working directory. You also have the Telegram chat history with the current user. Answer the user's question from memory; do not guess. If you cannot find it, say so.

{memory}

Finish your reply with exactly two lines:
SOURCES: <note paths and/or message ids you relied on, or none>
ANSWER: <1-3 sentences>"""

MEM_B = """LONG-TERM MEMORY (search_memory):
- You have search_memory - semantic search across the ENTIRE dialog history. It survives context compaction/reset, so details no longer in your current context are still findable.
- Call it when the user references the past ("помнишь", "что я говорил про…", "мы обсуждали", "как тогда") or when you need a detail that fell out of context after compaction.
- Pass a natural-language query (what you're looking for, not exact words). Optional: role ('user'/'assistant'), limit (default 5).
- Do NOT call it on every message - only when memory is actually needed. Casual chat ("привет", "ок") needs no search."""

MEM_A = """LONG-TERM MEMORY - search it as TEXT, like grep. Nothing here matches meaning for you: you choose the words.
Two stores:
1. Notes: markdown files in the working directory. Layout:
{layout}
   Search them with Grep (exact words, identifiers, numbers, dates, name stems; alternations like `фиолетов|сиренев`; several short patterns beat one long phrase) and Glob (dates are in file names). Read the located file before you answer.
2. Chat history of this chat ({hist}): the search_memory tool (case-insensitive regex over message text; returns id/time/role + snippet, newest first) and read_messages(id, before, after) for the surrounding messages. Use word stems, alternations, since/until dates, the role filter.
Method: guess 2-4 distinctive words the ORIGINAL text probably used (not the words of the question), search; too many hits -> add a second word or a date range; no hits -> another word form or synonym, or the other store; read the hit in full; answer from what you read. Do NOT search on every message - only when memory is actually needed."""

def layout_block(corpus: str) -> str:
    top = collections.defaultdict(list)
    for d, _, fs in os.walk(corpus):
        rel = os.path.relpath(d, corpus)
        for f in fs:
            top[rel.split(os.sep)[0] if rel != "." else "(root)"].append(os.path.join(rel, f))
    lines = []
    for t, fl in sorted(top.items(), key=lambda kv: -len(kv[1])):
        if t.startswith("."):
            continue
        sub = collections.Counter(os.path.dirname(p).split(os.sep)[1] if os.path.dirname(p).count(os.sep) else "" for p in fl)
        subs = ", ".join(f"{s}/ ({n})" for s, n in sub.most_common(6) if s)
        names = sorted(os.path.basename(p) for p in fl)
        lines.append(f"   - {t}/ - {len(fl)} files" + (f"; subfolders: {subs}" if subs else "") + f"; e.g. {names[0]}, {names[len(names)//2]}, {names[-1]}")
    return "\n".join(lines)

def hist_block(bench: str, chat: int) -> str:
    n, a, b = sqlite3.connect(f"file:{bench}/messages.db?mode=ro", uri=True).execute(
        "select count(*),min(timestamp),max(timestamp) from messages where chat_id=?", (chat,)).fetchone()
    return f"{n} messages, {a[:10]} .. {b[:10]}"
