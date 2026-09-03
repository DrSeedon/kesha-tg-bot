# Task #10 — File RAG: PLAN

**Scope (approved):** index `.md` + `.txt` from `WORK_DIR` (`/opt/cog-second-brain`) into the
existing RAG, with markdown heading-aware chunking, a live `watchfiles` watcher, sha256 dedup,
and `[file: path]` source attribution. **Do NOT** index xml/csv/json/html (measured as noise).
Do NOT break the dialog RAG.

## Design decisions (locked from research)

1. **Separate storage for files** — new `vec_files` vec table + `file_chunks` + `files` tables.
   Dialog tables (`vec_messages`, `fts_messages`, `indexed`) stay **byte-for-byte untouched** on
   the code path. Only reason schema bumps is the *new* tables get created; a version bump also
   safely rebuilds (established pattern).
2. **No chat partition for files** — files are shared knowledge. `vec_files` has no PARTITION KEY;
   file search runs unconditionally and is RRF-fused with the per-chat dialog results.
3. **Content lives locally, at CHUNK granularity** — file chunk text stored in
   `file_chunks(chunk_id, file_id, text)` and in `fts_files` (keyed by `chunk_id`). The read path
   fetches the **matched chunk's** text from there (NOT from messages.db). Unlike dialog search
   (which dedups to `parent_message_id` and returns the whole message), file search keeps chunk
   identity — a query matching one section of a 50-chunk file returns THAT section, not the file.
   *(Codex blocking fix — see codex-review-plan.md.)*
4. **Dedup by sha256, keyed by path** — `files(path PK, sha256, mtime, file_id)`. mtime = cheap
   pre-filter; sha256 = source of truth for "content changed".
5. **watchfiles** for the watcher; enqueue changes onto the existing `rag_executor` (SQLite
   thread-affinity is a hard constraint).
6. **Chunking:** markdown heading-aware (breadcrumb-prefixed sections, split big / merge tiny);
   `.txt` → paragraph split; fallback → existing char-window `_chunk`.

## Schema (SCHEMA_VERSION 7 → 8)

New tables (added alongside existing; on 7→8 the derived tables drop+rebuild as always):
```sql
-- file metadata + dedup
CREATE TABLE files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT UNIQUE NOT NULL,      -- relative to WORK_DIR
    sha256  TEXT NOT NULL,
    mtime   REAL NOT NULL
);
-- vec index for file chunks (NO chat partition — shared knowledge)
CREATE VIRTUAL TABLE vec_files USING vec0(
    chunk_id  INTEGER PRIMARY KEY,     -- file_id * CHUNK_STRIDE + idx
    file_id   INTEGER,
    embedding FLOAT[1024]
);
-- content store for read-path hydration (files aren't in messages.db)
CREATE TABLE file_chunks (
    chunk_id INTEGER PRIMARY KEY,      -- same id as vec_files
    file_id  INTEGER NOT NULL,
    text     TEXT NOT NULL
);
-- FTS for hybrid file search — keyed by chunk_id so the matched CHUNK is identifiable
CREATE VIRTUAL TABLE fts_files USING fts5(
    text, chunk_id UNINDEXED
);
```
`chunk_id = file_id * CHUNK_STRIDE + idx` (same STRIDE=1000). Separate table → no collision with
message chunk ids. **File FTS/vec return `chunk_id` (chunk-level), NOT `file_id`** — so RRF fuses
and the read path hydrates the exact matched chunk. (Dialog search dedups to parent_message_id;
file search does not — keeps chunk identity.)

## Config constants (config.py or rag.py)

- `KNOWLEDGE_DIR = Path(config.WORK_DIR)` — root to index/watch.
- `FILE_EXTENSIONS = {".md", ".txt"}`
- `EXCLUDED_DIRS = {".git", ".claude", ".gemini", ".kiro", ".github", ".serena", ".claude-plugin"}`
- `MD_MAX_CHUNK = 1500`, `MD_MIN_MERGE = 250` (from experiment).

## What NOT to touch
- `messages.db` — read-only, never written.
- `vec_messages` / `fts_messages` / `indexed` — dialog path unchanged.
- Existing `_chunk`, `index_message`, `backfill`, dialog `search` internals — extended, not rewritten.
- TG proxy / SSH tunnel / Ozon / reminders — irrelevant.

---

## Tickets

### T1 — File chunkers (pure functions) + tests
- **Files:** `rag.py` (add `_chunk_markdown`, `_chunk_text`, `_chunk_file(path, content)` dispatcher), `tests/test_rag_files.py`.
- **What:** markdown heading-aware splitter (breadcrumb-prefixed sections; split > `MD_MAX_CHUNK`
  by paragraph; merge < `MD_MIN_MERGE`); `.txt` paragraph splitter; dispatcher by extension with
  fallback to existing `_chunk`. Pure, no DB.
- **AC:**
  - markdown with `##`/`###` → each returned chunk starts on a heading/bullet/capital (no
    mid-sentence starts); a section under a heading keeps its breadcrumb prefix.
  - a heading-less string → falls back to paragraph/char split, never crashes.
  - empty / whitespace-only content → returns `[]`.
  - `.txt` splits on double-newline; single paragraph → one chunk.
  - oversized single word (URL/blob) → still capped (reuse `_split_oversized`).
- **blocked-by:** none

### T2 — File schema + index/delete/dedup (DB layer) + tests
- **Files:** `rag.py` (schema v8: `files`/`vec_files`/`file_chunks`/`fts_files`; methods
  `index_file(rel_path, content)`, `delete_file(rel_path)`, `_file_sha`), `tests/test_rag_files.py`.
- **What:** `index_file` — compute sha256; if path exists with same sha → no-op; else delete old
  chunks (if any) + chunk + embed + insert into vec_files/file_chunks/fts_files + upsert `files`.
  `delete_file` — remove all rows for path. SCHEMA_VERSION 7→8.
- **AC:**
  - index a markdown file → rows appear in `files`, `vec_files`, `file_chunks`, `fts_files`;
    counts consistent (one vec row per file_chunks row).
  - re-index same content → **no new rows**, sha256/file_id stable (idempotent).
  - re-index changed content → old chunks gone, new chunks present, single `files` row (dedup).
  - `delete_file` → all rows for that path removed from all 4 tables.
  - empty file → skipped, no rows.
  - existing dialog tables untouched (index a message + a file → both independently searchable).
- **blocked-by:** T1

### T3 — Unified search with source attribution + tests
- **Files:** `rag.py` (extend `search()` to fuse dialog + file results; file results carry
  `source='file'`, `path`, `content`), `tests/test_rag_files.py`.
- **What:** run existing dialog vec+fts search (dedup→`parent_message_id`) AND new file vec+fts
  search (**chunk-level, dedup→`chunk_id`, NOT file_id**). RRF-fuse with **namespaced keys**:
  dialog `("d", parent_message_id)`, file `("f", chunk_id)` — prevents id-space collision AND keeps
  file results at chunk granularity. Hydrate dialog results from messages.db (as today); hydrate
  each file result from `file_chunks WHERE chunk_id=?` + its `files.path`. Each result dict gets a
  `source` key: `'dialog'` (role/timestamp) or `'file'` (path).
- **AC:**
  - query matching one section of a multi-chunk file → result has `source='file'`, correct relative
    `path`, and the **matched chunk's** `content` (NOT the whole file).
  - query matching a dialog → result has `source='dialog'`, `role`, `timestamp`, `content` (unchanged shape).
  - a message_id and a file's chunk_id that happen to be equal integers → do NOT merge in RRF (namespaced keys).
  - a corpus with both → both types can appear, ranked by RRF.
  - `role` filter → **dialog-only**: when set, file results are excluded (files have no role). Documented in tool desc.
  - dialog-only corpus (no files) → identical behaviour to today (regression guard).
- **blocked-by:** T2

### T4 — search_memory tool formatting
- **Files:** `kesha_tools.py` (`search_memory` result rendering).
- **What:** render by source: file → `[file: {path}] {content}`; dialog → `[{timestamp} | {role}] {content}` (current). LIKE-fallback path stays dialog-only.
- **AC:**
  - file result rendered as `[file: 01-daily/2026-06-08-daily-dump.md] …`.
  - dialog result rendering unchanged.
  - no results → `No matches in history` (unchanged).
- **blocked-by:** T3

### T5 — backfill_files (startup one-time index) + tests
- **Files:** `rag.py` (`backfill_files()` — walk `KNOWLEDGE_DIR`, filter ext + excluded dirs, index each; skip unchanged via `files` mtime/sha).
- **What:** os.walk, prune excluded dirs, for each `.md`/`.txt`: read UTF-8 (skip on decode error, log), compute rel path, `index_file`. Batched, resumable (dedup makes re-runs cheap).
- **AC:**
  - fresh dir → all `.md`/`.txt` indexed, dot-dirs skipped, binaries skipped.
  - second run, no changes → 0 new rows (all deduped).
  - non-UTF8 file → skipped + logged, does not abort the walk.
  - deleted-on-disk file still in `files` (deleted while watcher was down) → **pruned** (delete its
    chunks). Confirmed via Codex suggestion — keeps index consistent after out-of-band deletes.
- **blocked-by:** T2

### T6 — watchfiles live watcher + bot.py wiring
- **Files:** `bot.py` (`_file_watcher()` asyncio task + startup call to `backfill_files`), `rag.py` (helper to map a change → index/delete), `requirements.txt` (+`watchfiles`).
- **What:** `asyncio.create_task(_file_watcher())`; `async for changes in awatch(KNOWLEDGE_DIR, watch_filter=…)`: for each `(Change, path)` → enqueue onto rag_executor: added/modified→`index_file`, deleted→`delete_file`. Debounce via watchfiles defaults. `backfill_files` via `asyncio.ensure_future` on startup (non-blocking, next to existing dialog backfill).
- **AC:**
  - create a `.md` in a temp watched dir → indexed within debounce window.
  - modify it → reindexed (dedup: only if content changed).
  - delete it → chunks removed.
  - a `.pdf`/`.json` change → ignored (watch_filter).
  - watcher runs on the rag_executor thread for all DB writes (no cross-thread sqlite).
  - `requirements.txt` has `watchfiles`.
- **blocked-by:** T2, T5 (T3/T4 independent — watcher needs index/delete + backfill)

---

## Test strategy
- `tests/test_rag_files.py` — mirrors `test_rag.py` style (real FastEmbed, tmp_path DBs). Covers:
  chunking per format (T1), index/dedup/delete (T2), file-vs-dialog unified search (T3),
  backfill incl. dot-dir/binary skip + non-UTF8 (T5). Watcher (T6): unit-test the change→action
  mapping; the awatch loop itself is thin glue (smoke-tested manually + a short awatch integration
  test with a real tmp dir if fast enough).
- Full run: `UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q` (per PROCESS RULES).

## Adversarial self-review (pre-Codex)
1. **chunk_id collision** — file `chunk_id = file_id*STRIDE+idx`. `file_id` autoincrements
   independently of message ids, and lives in a **separate** vec table → no collision with message
   chunk ids. But: a file with > STRIDE-1 chunks would collide with the next file_id. Cap chunks at
   STRIDE-1 (reuse existing pattern). Biggest real file ~50KB → ~35 chunks ≪ 1000. Safe, but cap anyway.
2. **RRF fusing two id-spaces + granularity** (Codex blocking) — dialog fuses on parent_message_id
   (whole message), files must fuse on **chunk_id** (matched section). Namespace keys `("d", mid)` /
   `("f", chunk_id)` — prevents int collision AND preserves file chunk identity. Handled in T3.
3. **watcher writes off-thread** — must enqueue onto rag_executor, never call sqlite from the
   awatch coroutine directly. Enforced in T6 wiring (same pattern as `_rag_worker`).
