**Blocking**
- `fts_files` deletion is likely wrong: `DELETE FROM fts_files WHERE chunk_id=?` targets an `UNINDEXED` column, but FTS5 deletes are rowid-based unless `chunk_id` is the rowid/contentless key. Old FTS file rows may survive after `delete_file`/update, causing deleted or stale chunks to be returned by FTS, then dropped at join time; quality degrades and stale FTS index grows. Fix by inserting `rowid=chunk_id` and deleting by `rowid`, or use external/content table correctly.

**Suggestions**
- Dialog search itself is not broken by schema v8: dialog tables are recreated as before; file search is additive and role-filtered out when `role` is set.
- RRF namespacing is correct: `("d", msg_id)` and `("f", chunk_id)` cannot collide even if integers match.
- File retrieval is chunk-level: both vec and FTS return `chunk_id`, then join to `file_chunks`.
- SHA dedup is mostly correct for unchanged content, but unchanged content with changed `mtime` is skipped, so metadata may stay stale; probably acceptable.
- Watcher sqlite writes go through `_rag.run(... apply_file_change ...)`, so thread-affinity is respected.
- `>999` file chunks do not collide because `_chunk_file` truncates to `CHUNK_STRIDE - 1`; possible truncation, not collision.
- Backfill count reports files updated, not chunks, despite wording; harmless.

APPROVE WITH FIXES
