# Codex review — Task #10 plan

Codex (GPT-5.5) reviewed `docs/tasks/10/plan.md`. It ran from the main repo checkout so the
sandbox blocked it from writing into the worktree path; the review content is transcribed here.

## BLOCKING — file FTS/RRF must be chunk-level, not file-level

**Finding:** The plan's `fts_files(text, file_id UNINDEXED)` and RRF fusion granularity are wrong.
Dialog search fuses on `parent_message_id` and hydrates the whole message from messages.db. But
**file search must return the specific matched chunk's text**, not the whole file. With
file-level identity, a query matching one section of a 50-chunk file would hydrate the entire file
(or be unable to pick which chunk) — breaking source attribution and dumping huge context.

**Fix (accepted):**
- File FTS carries **chunk identity**: `fts_files` uses `rowid = chunk_id` (or an explicit
  `chunk_id UNINDEXED` column), matching how `vec_files.chunk_id` is the PK.
- File vec + FTS both return `chunk_id`. **Do NOT dedup file candidates to file_id** the way dialog
  dedups to parent_message_id — keep chunk granularity so the matched section is what's returned.
- RRF keys are namespaced by source AND carry chunk identity: dialog `("d", parent_message_id)`,
  file `("f", chunk_id)`. This also resolves the id-space collision the plan already flagged.
- Read path hydrates each file result from `file_chunks WHERE chunk_id = ?` → returns that chunk's
  `text` + its file's `path`.

## Confirmed sound (no change)
- Separate `vec_files`/`file_chunks`/`files` tables — good; dialog RAG untouched.
- `chunk_id = file_id*STRIDE+idx` with cap at STRIDE-1 — no collision (separate table + capped).
- sha256+path dedup, mtime pre-filter — correct.
- Watcher must enqueue onto rag_executor (never sqlite off-thread) — correctly flagged in plan §3.
- Scope `.md`+`.txt` only — agreed.

## Suggestions (non-blocking)
- **Stale-file prune in backfill** — plan already marks this "decide". Recommend: yes, prune
  `files` rows whose path no longer exists on disk (keeps index consistent after out-of-band deletes
  while watcher was down). Cheap at this scale.
- **role filter semantics** — plan asks. Confirm: `role` filter is dialog-only; when a role filter
  is set, exclude file results (files have no role). Document in the tool description.
- **Empty/near-empty diary skip** — reuse existing `not content.strip()` guard; the 1061 diaries
  with `[[]]` placeholders produce low-signal chunks but skip-empty + RRF handle it. Optional
  min-real-chars threshold. Not blocking.

## Verdict
Plan is sound **after** making file FTS/RRF chunk-level (blocking fix above). Everything else is
scoped correctly for a 2-user / ~2600-chunk personal assistant.
