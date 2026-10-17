# V-45 stage 1 protocol (frozen by the commit that adds it, before any scored run)

**Question.** On questions to Kesha's memory worded differently from the source, does an agent with a *text* memory path (regex over messages + Grep/Glob/Read over notes + a layout "table of contents") answer as well as an agent with today's *semantic* `search_memory` (bge-m3 int8 + sqlite-vec + FTS5 + RRF)? Basis: Orchestra V-910 (topic index + `rg` found the record in 81 % vs hybrid top-5 61 % on KB/reports); its caveat is that Kesha's conversation history is a different corpus.

**Corpus (snapshot 2026-10-17 11:20 CEST, outside the repo in `/home/kesha/bench-V-45/`).** `messages.db`: online backup of prod `messages.db`, 12 867 messages, 2 chats (owner chat M, second user chat K), 2026-06-24..2026-10-17, avg length 340-1300 chars (not short). `corpus/`: the 2 363 `.md`/`.txt` files of `/opt/cog-second-brain` that the prod RAG index contains (the 59 files not in the index were removed so both arms see the same file set). `vec.db`: online backup of the prod RAG index taken in the same minute (no re-indexing; 20 files differ by sha from the index, they are not used as targets). Targets are *not* a random sample of the corpus: I (the author) read seed-45 random candidates (`sample.py`) and kept ones that carry a standalone retrievable fact; low-information chatter ("ок", photos, reminders) is excluded. This matches when memory is actually needed, but it is a selection by one author.

**Questions (40, `questions.py` + `reference.json`, kept private; sha256 in `frozen.sha256`).** 20 about messages (17 paraphrase P, 3 direct D), 20 about notes (18 P, 2 D); each asked from the chat that owns the target. Paraphrase = the question avoids the gold wording (checked by stem overlap, reported, not used to drop questions). `gold` regex per question; acceptable set = messages of the asking chat + note files whose text matches `gold` (the file-level and message-level truth).

**Arms (agent scenario, as Kesha really works).** Same model, same harness (`claude -p`, `--setting-sources ""`, cwd = `corpus/`, native tools restricted to Read/Grep/Glob = the `rg`/Read that Kesha's Bash+Read provide; Kesha's real CLAUDE.md topic index is NOT given to either arm), max 16 turns, no search budget, each question run in 2 repeats.
- **B (today)**: `mcp_sem.py` = the real `rag.RagMemory.search` (unchanged `rag.py`, HEAD of this branch == prod) over the copied index, same tool name/description/format as `kesha_tools.search_memory`; system prompt = today's LONG-TERM MEMORY block (`prompts.MEM_B`).
- **A (proposal)**: `mcp_text.py` (`search_memory(pattern, role, since, until, limit)` regex over this chat's messages, `read_messages`) + a layout block generated from the corpus + method text (`prompts.MEM_A`).
- Model: **Sonnet 5.5 as a proxy** for Kesha's Opus 5.5 (cost/quota); the real runtime model was not measured. Codex runtime not measured.

**Metrics.** Primary = *answer correct*, judged blind (arm hidden) by a model of another family (Luna) given the question, the gold excerpt from the target and the agent's final answer; per question mean over the 2 repeats. Secondary: `retrieved` (gold regex appears in any tool result), search/read call counts, paid tokens (input+cache-write+output), seconds, pure hybrid top-5/top-10 retrieval (B only, no agent). Runs with `outside_cwd` tool paths or rc≠0/empty final are rerun once; remaining failures count as incorrect and are reported.

**Decision rule (fixed).** d_q = A_q − B_q over P questions (history P = 17, notes P = 18, pooled P = 35).
- **STOP (semantic search notably better on history)** iff history-P mean d ≤ −0.15 AND one-sided exact sign-flip p < 0.10 (H0: mean d ≥ 0).
- **PROCEED to stage 2** iff the STOP condition does not hold AND pooled-P mean d ≥ −0.10.
- Otherwise (pooled d < −0.10 without a significant history effect) the "not worse within noise" condition is not shown → STOP with numbers.
Also reported, not decisive: bootstrap 95 % CI of d, per-source split, D controls, cost.

**Limits stated in advance.** One author of questions and of the text-tool prototype; n = 17-18 per source (detects ≈ ±0.2, not ±0.1); Sonnet proxy; hit judged by a model; the prototype text tool is the design that stage 2 would ship; Kesha's CLAUDE.md index (present in production for both paths) is excluded from both arms.
