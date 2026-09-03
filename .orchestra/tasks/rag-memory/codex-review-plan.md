---
slug: plan-review
topic: RAG memory plan review
model: gpt-5.5 (Codex)
created: 2026-06-26
---

## Summary

Codex (GPT-5.5, read-only sandbox) отревьюил `plan.md`. План в целом попадает в верные точки интеграции (`message_log.py`, `kesha_tools.py`, `bot.py main()`), но нашёл 4 blocking-проблемы concurrency/схемы. Все 4 проверены Claude по коду и приняты как валидные. План обновлён.

Read-only sandbox не дал Codex записать файл сам — findings взяты из stdout, этот файл составлен Claude по выводу Codex.

## Findings

### blocking #1 — синхронный embedding/search внутри async event loop
`kesha_tools.py` `search_memory` — `async def`, но звал `rag.get_rag().search()` синхронно. Embed (~15-40мс) + brute-force скан **замораживают event loop для всех чатов**.
→ **FIXED:** обёрнуто в `await loop.run_in_executor(rag._executor, ...)`.

### blocking #2 — шаринг RagMemory singleton между loop и to_thread
SQLite-коннект не thread-safe; план дёргал `rag.get_rag()` и из воркера, и из тула через разные потоки → race/corruption.
→ **FIXED:** единый `ThreadPoolExecutor(max_workers=1)`, RagMemory создаётся и живёт ТОЛЬКО в этом потоке. Все операции (index/search/backfill) через него. Один поток владеет коннектом — шаринга нет.

### blocking #3 — sqlite-vec/FTS5 DDL не проверен рабочим
DDL в плане был иллюстративным, ни разу не прогнан.
→ **FIXED:** добавлен шаг 0 (smoke-DDL прогон ДО написания ядра) + проверено локально: sqlite 3.46.1, FTS5 yes, enable_load_extension yes. План B если PARTITION KEY не поддержан установленной версией sqlite-vec.

### blocking #4 — противоречивая FTS5-схема content='' при требовании хранить content + фильтр chat_id
Черновик объявлял contentless FTS5 (`content=''`), но требовал и хранить content, и фильтровать по chat_id.
→ **FIXED:** обычная FTS5-таблица (`content`, `chat_id UNINDEXED`, `message_id UNINDEXED`). Дублирование текста для BM25 неизбежно и приемлемо (десятки MB на 100K).

## Verdict

Round 1: **требует доработки** (4 blocking).
После фиксов Claude: все 4 закрыты, план обновлён. Готов к Phase 3 (реализация), при условии что шаг 0 (smoke-DDL) подтвердит работу sqlite-vec на целевой версии.
