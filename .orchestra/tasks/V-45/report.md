# V-45 stage 1 report: text memory search vs semantic `search_memory` on Kesha's corpus

## Question
On questions worded differently from the source, does an agent with a *text* memory path (regex over chat messages + Grep/Glob/Read over notes + a layout block) answer as well as an agent with today's semantic `search_memory` (bge-m3 int8 + sqlite-vec + FTS5 + RRF)? Protocol, decision rule and question hashes were committed before the scored runs (`protocol.md`, `frozen.sha256`, commit 18bcfda).

## Corpus and its limits
Snapshot of 2026-10-17 (outside git, `/home/kesha/bench-V-45/`): prod `messages.db` (12 867 messages, 2 chats, Jun–Oct 2026, avg 340–1300 chars, so not "short replies") and the 2 363 `.md/.txt` notes that the prod RAG index contains; the prod `vec.db` copied in the same minute, no re-index. 40 questions: 17 paraphrase (P) + 3 direct (D) on messages, 18 P + 2 D on notes. Targets are not a uniform sample: from seed-45 random candidates I kept those carrying a standalone fact (chatter, photos and reminders excluded). One author wrote the questions. Lexical overlap with the source is not zero (stem check): several P questions share topical words with the target, so this is paraphrase, not synonym-only retrieval. Raw questions/answers stay private; this report has aggregates only.

## Arms
Agent scenario, Sonnet 5.5 as a proxy for Kesha's Opus 5.5 (Codex runtime not measured), `claude -p`, native tools Read/Grep/Glob only (Kesha's Bash+`rg` is equivalent; her real CLAUDE.md topic index is given to neither arm), 2 repeats per question, no search budget.
- **B (today)**: the real `rag.RagMemory.search` (unchanged `rag.py`) behind an MCP tool with the identical description/format, today's prompt section.
- **A (proposal)**: prototype `search_memory(pattern, role, since, until, limit)` regex over the chat + `read_messages(id, before, after)`, a generated layout block of the notes, and a method paragraph (`prompts.MEM_A`).
Primary metric: answer correct, judged blind by Luna against the gold excerpt. Two runs failed on infrastructure (CLI rc=1; an empty final) and were rerun once per protocol; no run touched paths outside the notes directory.

## Numbers (`analysis.txt`)
| set | n | A text | B semantic | d = A−B | 95 % CI |
|---|--:|--:|--:|--:|---|
| history P | 17 | 0.794 | 0.824 | −0.029 | [−0.118, +0.059] |
| notes P | 18 | 0.861 | 0.917 | −0.056 | [−0.194, +0.056] |
| pooled P | 35 | 0.829 | 0.871 | −0.043 | [−0.129, +0.029] |
| D (5 direct) | 5 | 0.90 | 1.00 | | |
Retrieval (gold string appeared in any tool result): pooled P A 0.814, B 0.871. Behaviour per run: A 3.75 search + 0.99 read calls, B 2.27 search + 0.09 read; paid tokens 9.2K vs 9.8K; 14.9 s vs 15.8 s. Pure hybrid without an agent (question text as the query, top-5/top-10 chunks): pooled P 0.49/0.66, history P 0.53/0.71, notes P 0.44/0.61 — about the same as V-910's 61 % top-5; the agent's own re-querying lifts the same index to 0.87.

## Verdict by the frozen rule
STOP needed history d ≤ −0.15 with p < 0.10: observed −0.029 (p≈0.5) → not fired. PROCEED needed pooled d ≥ −0.10: observed −0.043 → met. **Text path is not worse than the hybrid within noise → stage 2 proceeds.**

## Counter-examples and caveats
- Semantic search won single questions: a note about a server kill where the text agent never found the log line (A 0 vs B 1), a few where the text agent needed more query attempts (M12, M20, F14: 0.5 vs 1.0). Text won 2 (M06, F12) and lost 4 (M12, M20, F03, F14); ties dominate (29 of 35 P questions equal), so the test has resolution of roughly ±0.15–0.2, not ±0.05. A true semantic advantage of ~0.1 is not excluded.
- Both arms failed together on 3 P questions (M03, M13, F10): questions whose key words are generic or whose facts repeat across many records.
- The text arm costs ~1.5 more calls per answer; tokens and latency are equal.
- The text prototype was written by me, tuned only on the single pilot question; it is the design stage 2 ships.

## Reproduction note
`mcp_sem.py` and `pure_topk.py` import `rag.py`, which stage 2 deleted from the tree; to rerun them check out `rag.py` from commit 18bcfda into a directory and pass it as `RAGSRC`.

## Stage 2 (done after the verdict)
Replaced the semantic path: `memory_search.py` + tools `search_memory`/`read_messages` in `kesha_tools.py` (also registered on the Codex bridge), rewritten memory section of `system_prompt.txt`, removed `rag.py`, `tests/test_rag*.py`, the indexing callback in `message_log`/`bot.py` and the `fastembed`/`sqlite-vec`/`watchfiles` requirements. New `tests/test_memory_search.py`: chat isolation, alternation and ordering, role/date filters, bad-regex fallback, bounded output, neighbours stay in chat, tool resolves the chat from context. Mutation: changing the SQL to `chat_id>=0` turned 3 of those tests red (`test_other_chat_is_never_returned`, `test_alternation_matches_stems_newest_first`, `test_tool_uses_current_chat_not_args`). Native tools in production (checked on Kesha's own session logs): Bash, Read, WebSearch, MCP tools are used; Claude runs with `bypassPermissions` and no tool allow-list, Codex with `danger-full-access`, so `rg` is available on both and no duplicate file-search tool was written.
Deployment (not done, owner): `git pull` + restart; `storage/vec.db*` can then be removed by hand.
