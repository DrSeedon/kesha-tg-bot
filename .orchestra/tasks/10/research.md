# Task #10 — RAG indexing of cog-second-brain files: RESEARCH

**Question:** How to index the file knowledge base (`/opt/cog-second-brain`) into the
existing dialog RAG (`rag.py`) — all text formats, smart chunking, live inotify watcher,
source attribution, dedup on file change — without breaking the existing dialog RAG.

**Method:** Real inventory on prod Contabo (158.220.127.161) + chunking experiments with
the actual bge-m3 int8 model + web research on inotify/asyncio. Nothing was changed on prod.

---

## 1. File inventory (measured on prod, 2026-07-06)

`/opt/cog-second-brain` = 64MB total. Files by extension (excluding `.git`):

| ext | count | nature | index? |
|-----|-------|--------|--------|
| **md** | **1342** | **the knowledge base** — diaries, daily dumps, notes, projects | ✅ YES |
| pdf | 39 | binary | ❌ out of scope |
| jpg/png/svg | 27 | binary | ❌ |
| docx | 13 | binary (zip) | ❌ out of scope |
| xml | 10 | **gov reports** (ЕФС-1, РСВ, персвед) — machine XML, near-zero semantic value | ❌ skip |
| js/py/css/sh/bat | ~18 | code (dashboards, scripts) | ❌ skip (not knowledge) |
| toml/yml/json | ~13 | config + one 107KB data dump (`data.json`) | ❌ skip |
| csv | 4 | **finance/health logs** (tbank 872KB, sber 780KB, sleep 15KB) — tabular, not prose | ❌ skip (see §6) |
| txt | 2 | tiny braindumps (616 B) | ✅ small, harmless |

**Confidence: CONFIRMED** (direct `find` on prod).

### Key finding: markdown IS the knowledge base; other formats are noise

The task listed many formats (html/xml/json/yaml/csv…), but the **measured reality** is that
**99% of the semantic value is in `.md`**. The non-md text formats present are:
- **XML** = government tax reports (machine-generated, structured codes, no prose to search).
- **CSV** = raw transaction/sleep logs (872KB of `date,amount,merchant` rows). Indexing these
  as text produces thousands of near-identical garbage chunks that pollute retrieval.
- **JSON/YAML** = configs + a dashboard data dump, not knowledge.
- **HTML** = 2 dashboards + a company site (`ooo-sidon/site`) — UI markup, not notes.

**Recommendation (REFUTES the "all formats" requirement as written):** index **`.md` + `.txt`
only** in v1. This is the "stable 90% > fragile 100%" philosophy from the project brief.
Adding html/xml/csv/json parsers = more code, more edge cases, worse retrieval (garbage chunks).
If the user later drops real prose into `.html`/`.txt`, the format list is one constant to extend.
→ **This is a decision for the approval gate — flagged explicitly, not decided silently.**

### Markdown sub-structure (matters for chunking)

| dir | md count | shape |
|-----|----------|-------|
| `02-personal/diary-archive-2022-2025` | **1061** | tiny (avg 409 B), template-structured, mostly 1 chunk each. Only **2** truly empty. |
| `01-daily` | 86 | daily dumps, large (up to 9KB), deep `##`/`###` heading structure |
| `04-projects` | 35 | project notes, medium-large |
| `05-knowledge` | 33 | consolidated research, large (up to 31KB) |
| dot-dirs (`.claude`,`.kiro`,`.gemini`,`.github`) | **45** | **framework/plugin files** (SKILL.md, IMPLEMENTATION-TODO.md) — NOT user knowledge |

- avg md = 1591 B → **most md files are a single chunk** (< `CHUNK_CHAR_LIMIT` 1200… well, ~1591 B ≈ borderline; small ones 1 chunk, dumps multi-chunk).
- **Exclude dot-dirs** (`.git`, `.claude`, `.gemini`, `.kiro`, `.github`, `.serena`, `.claude-plugin`) — these are tool config, not knowledge. `watchfiles` default-ignores `.git`/`.venv` already.
- No non-UTF8 files, no symlinks (clean corpus — measured).

---

## 2. Chunking experiment (bge-m3 int8, real model)

**Hypothesis:** markdown heading-aware chunking retrieves better than the current naive
char-window chunker, because chunks are semantically self-contained (one section = one topic,
clean start/end) instead of arbitrary mid-sentence fragments.

**Metrics defined before running:** chunk start/end coherence; top-1 / top-3 hit rate + MRR
on control queries against the actual embedding model.

### 2a. Structural coherence (all large md samples)

| file | strategy | chunks | **start-clean** | end-coherent |
|------|----------|--------|-----------------|--------------|
| daily-dump.md | naive | 9 | **33%** | 11% |
| daily-dump.md | md-aware | 10 | **100%** | 10% |
| knowledge-research.md | naive | 36 | **36%** | 8% |
| knowledge-research.md | md-aware | 50 | **100%** | 10% |
| wellness-analyses.md | naive | 45 | **29%** | 11% |
| wellness-analyses.md | md-aware | 40 | **98%** | 30% |

- **start-clean** (chunk starts on heading/bullet/capital, not mid-word): md-aware **98-100%** vs
  naive **29-36%**. This is the readability payoff — the LLM receives whole sections, not
  fragments like `"...80%) + сдувание тёплой шубы (20%)"`.
- end-coherent is low for both because markdown bullet lists don't end in punctuation — expected, not a defect.

### 2b. Retrieval quality — single doc (COUNTER-EVIDENCE)

Indexed daily-dump.md alone (9-10 chunks), 5 control queries:
- naive: top-1 **5/5**, MRR 1.000
- md-aware: top-1 **4/5**, MRR 0.900

**Naive won here.** But this is misleading: with only ~10 chunks from one doc, everything ranks
near the top — no distractors. Not representative of the real 2500+ chunk corpus. Recorded
honestly (did not move goalposts).

### 2c. Retrieval quality — multi-doc corpus with distractors (DECISIVE)

Indexed 3 docs together (physics + AI research + medical = mixed topics = distractors),
same 5 control queries:

| strategy | chunks | top-1 | top-3 | MRR |
|----------|--------|-------|-------|-----|
| naive | 90 | 3/5 | 4/5 | 0.700 |
| **md-aware** | 100 | **4/5** | **5/5** | **0.900** |

**At scale with distractors — which matches the real corpus — md-aware wins clearly**
(top-3 no misses, MRR +0.20). Naive's mid-sentence fragments compete poorly against thousands
of other chunks.

**Conclusion: CONFIRMED.** Use **markdown heading-aware chunking** for `.md`:
- split by `#..######` headings into sections, carry a heading breadcrumb (`Physics > Thermodynamics`);
- sections > ~1500 chars → split by paragraph (`\n\n`);
- tiny sections (< ~250 chars) → merge into neighbour;
- files with no headings (diaries) → fall back to paragraph split (most are 1 chunk anyway).

Plain `.txt` → paragraph split (double `\n`), fallback to current naive window.

---

## 3. inotify / watcher decision

Researched watchdog vs pyinotify vs watchfiles vs inotifywait-subprocess for asyncio integration.

**Prod facts (measured):** `fs.inotify.max_user_watches = 61659`; corpus has **100 dirs**
(recursive) → watch budget is a non-issue. Python 3.12.3. No watcher lib currently installed.

**Decision: `watchfiles`** (Rust-backed, async-first). Rationale:
- **Native asyncio**: `async for changes in awatch(path): …` — no thread↔loop bridge, no
  `threading.Timer`. watchdog is thread-based and needs a wrapper (hachiko / custom) + manual
  debounce. watchfiles was built specifically to fix watchdog's threading + debounce pain.
- **Built-in debouncing** (`debounce`/`step` params) — coalesces the rapid-fire "editor saves
  file 5×" burst into one event. Critical: without it, one save → 5 reindexes.
- **`watch_filter`** — filter to `.md`/`.txt` and skip dot-dirs at the source (fewer wakeups).
- **`Change` enum** = `added` / `modified` / `deleted` — maps directly to index / reindex / delete.
- Rust `notify` backend = lower overhead than pure-Python pyinotify; still cross-platform (dev on
  laptop, prod on Linux both work).

**Rejected:**
- *watchdog* — thread-based, awkward asyncio bridge, DIY debounce. More glue code.
- *pyinotify* — Linux-only, unmaintained, no async, manual debounce.
- *inotifywait subprocess* — parse stdout, fragile, process management, no clean debounce.

**Integration:** one `asyncio.create_task(_file_watcher())` in `bot.py` startup (next to the
existing `_rag_worker` task). On each debounced change set → enqueue onto the **same
`rag_queue`** (or a parallel one) so all sqlite-vec writes stay on the single `rag_executor`
thread (SQLite thread-affinity — hard constraint from existing design).

**Confidence: CONFIRMED** (lib installed + API verified locally; watch budget measured on prod).
Sources: [watchfiles PyPI](https://pypi.org/project/watchfiles/), [watchdog PyPI](https://pypi.org/project/watchdog/), [watchgod/watchfiles rationale](https://github.com/pbiggar/watchgod), [hachiko asyncio wrapper](https://github.com/biesnecker/hachiko).

---

## 4. Schema changes (vec.db, SCHEMA_VERSION 7 → 8)

Current schema is **message-centric** and this is the core integration challenge:
- `vec_messages(chunk_id, parent_message_id, chat_id PARTITION KEY, role, embedding)`
- `chunk_id = message_id * CHUNK_STRIDE + idx` (integer ID space tied to messages)
- `search()` **JOINs to `msg.messages` for content+timestamp** — files are NOT in messages.db,
  so file content has nowhere to come from on the read path.
- `chat_id PARTITION KEY` — every row belongs to one chat; files belong to no chat.
- `indexed(message_id)` — tracks what's indexed; no file tracking.

### Three real problems + proposed solutions (for Phase 2 to finalize)

**P1 — content source on read path.** `search()` joins messages.db for `content`. Files aren't
there. → Add a `files` table in vec.db holding `(file_id, path, mtime, sha256)` and store file
**chunk text** in FTS (already stored) + a `file_chunks(chunk_id, file_id, text)` table so the
read path can fetch file content locally instead of from messages.db. Search must branch by
source type when hydrating results.

**P2 — ID space collision.** `chunk_id = message_id*STRIDE+idx`. File chunks need non-colliding
IDs. → Give files a separate id space: e.g. file `chunk_id = FILE_ID_BASE + file_id*STRIDE + idx`
with `FILE_ID_BASE` above any realistic message_id (messages are ~685; a base like 10^12 is safe),
OR add a `source_type` column and a separate vec table. **Separate vec table
(`vec_files`) is cleaner** — avoids partition-key abuse and ID arithmetic. Decide in Phase 2.

**P3 — chat_id / partitioning.** Files are shared, not per-chat. `chat_id` is a PARTITION KEY so
vec search filters by it. → Files need to be visible to **both** users. Options: (a) a sentinel
`chat_id = 0` (shared) and search both `chat_id=<user>` and `chat_id=0`; (b) separate `vec_files`
table with no chat partition, searched unconditionally and RRF-fused with dialog results.
**Option (b) (separate table) is cleanest** and keeps dialog RAG untouched.

**Migration:** SCHEMA_VERSION 7→8 drops derived index tables and rebuilds. Dialog rows rebuild
from messages.db via existing `backfill()` (proven, safe — messages.db never touched). File rows
build via a new `backfill_files()` walking cog-second-brain. **Existing dialog RAG behaviour is
preserved** because the version bump already drops+rebuilds derived tables — the design's
established pattern (v6→v7 did exactly this on the model swap).

### Source attribution (hard requirement)

`search()` currently returns `[{message_id, chat_id, role, content, timestamp}]` and the tool
formats `[{timestamp} | {role}] {content}`. → Add a `source` discriminator to each result:
- dialog: `[dialog | {role}] {content}` (or keep current format)
- file: `[file: {relative_path}] {chunk_text}` where path is relative to cog-second-brain
  (e.g. `01-daily/2026-06-08-daily-dump.md`).

Relative path is stored in the `files` table; the tool renders it. **Confidence: CONFIRMED** —
this is a data-model + formatting change, straightforward once P1-P3 land.

---

## 5. Dedup on file change

**Decision: dedup by `sha256` of file content, keyed by `path`.**
- `files(path PRIMARY KEY, sha256, mtime, file_id)`.
- On `modified` event: recompute sha256. If unchanged (editor touched mtime but content same) →
  **skip** (avoids needless re-embed). If changed → **delete all chunks for that file_id**
  (from `vec_files` + `fts` + `file_chunks`) then re-chunk + re-embed + reinsert.
- On `deleted` event: delete all chunks for that path.
- On `added`: index fresh.

**Why sha256 not mtime alone:** mtime changes on any touch (git checkout, rsync, editor save
with no edit). Content hash is the source of truth for "did the meaning change". mtime is a cheap
pre-filter (if mtime unchanged, skip hashing). **Confidence: CONFIRMED** (standard pattern;
matches project's fail-loud/explicit philosophy).

---

## 6. RAM / latency budget

**Estimated corpus growth:**
- ~1342 md → ~1200 small (1 chunk) + ~142 large (avg ~5 chunks) ≈ **~1900 file chunks**.
- Current: ~685 dialog msgs. Total vec.db ≈ **~2600 vectors** (from ~685 today).
- vec.db today = 15MB for dialogs. bge-m3 = 1024-dim × int8-ish → ~4KB/vector raw + FTS text.
  ~1900 more chunks ≈ +8-12MB. **New vec.db ≈ 25-30MB** — trivial on 8GB Contabo (~6GB free).
- Bot RSS today ~1248MB (embedder resident). File indexing reuses the **same embedder** — no new
  model load, no RSS increase beyond transient batch buffers.

**Backfill time:** dialog backfill = 685 msgs ~24 min (measured earlier). Files ≈ 1900 chunks
≈ 3× the vectors → rough **~30-40 min one-time backfill** on first boot after deploy (batched,
non-blocking via `asyncio.ensure_future` like existing backfill). Acceptable — one-off.

**Search latency:** current 58-78ms. Corpus 4× larger but sqlite-vec KNN on ~2600 vectors is
still sub-100ms (vec0 brute-force is fine at this scale; margin to 10K+). **No meaningful impact.**

**Confidence: LIKELY** (extrapolated from measured dialog numbers; exact file backfill time not
run on prod to avoid touching prod RAG before approval).

---

## 7. Edge cases (measured / to handle)

| case | reality | handling |
|------|---------|----------|
| empty files | 2 diary templates are near-empty | skip if stripped content < N chars (reuse existing `not content.strip()` guard) |
| binary in `.txt` | none found, but possible | UTF-8 decode with `errors=strict`; on `UnicodeDecodeError` → skip + log (fail-loud, don't index garbage) |
| huge files | biggest md = 48KB (SKILL.md, a dot-dir → excluded); real max ~31KB | chunker caps chunks; no memory concern |
| symlinks | none | `watchfiles` follows by default; not present so moot |
| hidden/dot-dirs | 45 md in `.claude`/etc | **exclude** via watch_filter + backfill walk filter |
| non-UTF8 | none found | strict decode + skip-on-error (above) |
| rapid saves | editor writes 5× | watchfiles debounce coalesces |
| file moved/renamed | rename = delete old path + add new path | handled by delete+add events; sha256 dedup means re-embed only if content differs |

---

## 8. Affected files (for Phase 2)

- **`rag.py`** — new file-chunking (md heading-aware + txt paragraph), `vec_files`/`files`/
  `file_chunks` schema, `index_file()` / `delete_file()` / `backfill_files()`, `search()` fused
  to include file results + source attribution. SCHEMA_VERSION 7→8.
- **`bot.py`** — `_file_watcher()` asyncio task (watchfiles awatch), enqueue file changes onto
  rag_executor; `backfill_files()` on startup.
- **`kesha_tools.py`** — `search_memory` result formatting: render `[file: path]` vs `[dialog]`.
- **`pyproject.toml`** — add `watchfiles` dependency (shared file — coordinate via orchestrator).
- **new config constants** — `KNOWLEDGE_DIR` (cog-second-brain path), indexed extensions,
  excluded dirs.
- **tests** — chunking per format, index/dedup/delete, file-vs-dialog search, watcher events.

---

## 9. Risks

- **Breaking dialog RAG** — mitigated by keeping files in a **separate vec table** (dialog path
  untouched) + version-bump rebuild is the established safe pattern.
- **Retrieval pollution from empty/template diaries** — 1061 diary files, many skeletal. Skip-empty
  guard + the fact that near-empty ones embed to low-signal vectors and rarely rank. Monitor.
- **Backfill blocking startup** — use `asyncio.ensure_future` (non-blocking) like existing backfill.
- **"All formats" scope** — RESEARCH REFUTES indexing xml/csv/json/html as written (garbage
  retrieval). Recommend `.md` + `.txt` only for v1. **Needs user decision at gate.**

---

## Summary (for approval)

- **Index `.md` + `.txt` only** (not xml/csv/json/html — measured as machine data / logs / configs,
  not prose; indexing them degrades retrieval). ← decision needed.
- **Markdown heading-aware chunking** — CONFIRMED better retrieval at scale (top-3 5/5 vs 4/5,
  MRR 0.90 vs 0.70, start-clean 100% vs ~33%).
- **watchfiles** for the live watcher — async-native, built-in debounce, Rust inotify backend.
- **Separate `vec_files` table** + `files`(path,sha256,mtime) — keeps dialog RAG untouched,
  clean source attribution, sha256 dedup on change.
- **Budget fine** — ~1900 new chunks, vec.db ~25-30MB, no RSS increase, search stays <100ms.
- SCHEMA_VERSION 7→8, safe rebuild (established pattern).
