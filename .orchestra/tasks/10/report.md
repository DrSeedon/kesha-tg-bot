# Task #10 — File RAG indexing: REPORT

## What shipped
Index the file knowledge base (`WORK_DIR` = `/opt/cog-second-brain`, `.md`+`.txt`) into the
existing dialog RAG, unified search with source attribution, markdown heading-aware chunking,
sha256 dedup, and a live `watchfiles` watcher. Dialog RAG untouched (separate tables).

## Files changed
| file | ± | what |
|------|---|------|
| `rag.py` | +390 | file chunkers (`_chunk_markdown`/`_split_paragraphs`/`_chunk_file`), schema v8 (`files`/`vec_files`/`file_chunks`/`fts_files`), `index_file`/`delete_file`/`apply_file_change`, `_vec_search_files`/`_fts_search_files`, namespaced `_rrf`, unified `search()`, `backfill_files`, `file_change_target`, `KNOWLEDGE_DIR` |
| `bot.py` | +22 | `_file_watcher()` asyncio task + `backfill_files` on startup (both via rag_executor) |
| `kesha_tools.py` | +18 | `search_memory` renders `[file: path]` vs `[ts | role]`; description updated |
| `requirements.txt` | +1 | `watchfiles>=0.24` |
| `tests/test_rag_files.py` | new | 27 tests |

## Tickets (all done)
- **T1** file chunkers — md heading-aware + txt paragraph + dispatcher. AC met.
- **T2** file schema + index/delete/dedup — sha256, idempotent, replace-on-change. AC met.
- **T3** unified search + source attribution — namespaced RRF, chunk-level file hydration. AC met.
- **T4** `search_memory` formatting — `[file: path]`, role=dialog-only. AC met.
- **T5** `backfill_files` — walk, dot-dir/binary skip, non-UTF8 skip, dedup, prune stale. AC met.
- **T6** watchfiles watcher + wiring — `file_change_target` filter, `apply_file_change` on executor. AC met.

## Design (key decisions)
- **Separate file tables** — dialog path byte-for-byte unchanged; only new tables added. Schema
  7→8 bump drops+rebuilds derived index (established pattern; messages.db never touched).
- **Chunk-level file retrieval** (Codex plan-review fix) — file FTS keyed by `chunk_id`, RRF fuses
  `("f", chunk_id)` — matched section returned, not whole file. Dialog fuses `("d", msg_id)`.
  Namespacing prevents int-collision between the two id-spaces.
- **No chat partition for files** — files are shared; `role` filter excludes files (dialog-only).
- **sha256 dedup** keyed by path; mtime stored. Unchanged content → no-op re-embed.
- **All sqlite writes on rag_executor** — watcher enqueues via `_rag.run` (thread-affinity).

## Tests
44 pass (17 existing dialog + 27 new file), run sequentially per file (combined run OOMs the box —
embedder + full suite; not a code issue). Covers: chunking per format, index/dedup/delete, unified
search (file vs dialog, chunk-level, namespaced RRF, role filter, dialog regression), backfill
(skip dot-dir/binary/non-UTF8, prune), watcher change→action mapping.

## Adversarial self-review
- **chunk_id collision > 999 chunks/file** — capped at `CHUNK_STRIDE-1` in `_chunk_file`. Biggest
  real file ~50KB → ~35 chunks ≪ 1000. Safe.
- **RRF id collision** — solved by namespaced keys (tested).
- **watcher off-thread sqlite** — enqueued via executor, never direct.
- **OOM** — reuses existing embedder (no new model), ~1900 new chunks, vec.db ~15→~28MB. Backfill
  batched/non-blocking. Combined test-run OOM is a test-harness artifact, not runtime.

## Breaking / deploy notes
- **Breaking:** none to dialog RAG behaviour. Schema 7→8 auto-rebuilds file+dialog derived index
  on first boot (dialog from messages.db, files from disk) — one-time ~30-40 min backfill.
- **Deploy (T5/T6 need prod):** `pip install watchfiles` on Contabo, then restart bot. WORK_DIR
  already = /opt/cog-second-brain. **Ping orchestrator before bot restart** (per instructions).

## TODO / follow-ups
- Monitor retrieval pollution from ~1061 near-empty diary templates (skip-empty guard handles most).
- watchfiles debounce uses defaults — tune if editor-save bursts cause redundant reindex (unlikely).

---

## Post-deploy optimization (#10-opt) — search no longer blocks on backfill

**Problem:** search_memory hung 300s during backfill → reconnect. Single `rag_executor`
(max_workers=1) serialized everything; backfill embed batches blocked search in the queue.

**Profiled root cause:** embed = 82ms/chunk vs insert = 0.6ms/chunk (**138×**). Bottleneck is
CPU-bound embedding, not SQLite.

**Fix (all empirically verified before shipping):**
- **Read/write executor split.** search → RO connection (`?mode=ro`) on its own thread; index/backfill
  → RW connection. WAL gives lock-free concurrent reads (measured: 37 vec0 reads during active writes,
  0 errors, 0.14ms). `run()` routes `search` → read executor, everything else → write executor.
- **Shared module-level embedder singleton.** ONNX `InferenceSession.run()` is thread-safe (measured:
  15+15 concurrent embeds, 0 errors) → both threads share ONE embedder, no +1.3GB RAM.
- **batch_size 16→64.** Embed throughput 113→98ms/chunk (curve bottom; 128 regresses).

**E2E proof:** search latency during heavy concurrent backfill = **37-41ms** (was ~300,000ms). ~7300×.

**Files:** rag.py (RO conn, `_get_embedder` singleton, `get_rag_ro`, routed `run`, EMBED_BATCH),
bot.py (`_rag_read_executor`). Tests: +3 (RO sees writes+searches, RO can't write, run routing).
47 tests green (17 dialog + 30 file).

**Deploy:** needs merge to main + prod pull + bot restart. No new dependency.
