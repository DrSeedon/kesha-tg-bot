# RAG-память для Kesha — final report

**Задача:** семантический поиск по всей истории диалогов (переживает compact). Стек: sqlite-vec + FastEmbed (multilingual MiniLM) + гибрид FTS5/вектор (RRF) + MCP tool `search_memory`.

## Что сделано

Добавлена долговременная RAG-память. Все сообщения (`messages.db`) индексируются в отдельную `storage/vec.db` (sqlite-vec + FTS5). Кеша ищет по ней через тул `search_memory` — семантика + ключевые слова, слияние через RRF. Индексация инкрементальная (фоновый поток, не блокирует ответы) + разовый backfill существующей истории при старте.

## Файлы

| Файл | Δ | Что |
|------|---|-----|
| `rag.py` | **новый**, +258 | Ядро: `RagMemory` (embedder + sqlite-vec + FTS5 + RRF + backfill + schema-version migration), `run()` executor-хелпер |
| `tests/test_rag.py` | **новый**, +130 | 10 тестов (index/search/isolation/idempotency/role-filter/RRF/migration) |
| `message_log.py` | +37/-8 | `log_user/log_assistant` возвращают id + `on_message` callback для индексации |
| `kesha_tools.py` | +35 | `@tool search_memory` + регистрация в `tools=[...]` |
| `bot.py` | +33 | executor (1 поток) + очередь + фоновый воркер индексации + backfill + shutdown |
| `system_prompt.txt` | +9 | инструкция Кеше когда звать `search_memory` |
| `requirements.txt` | +2 | `sqlite-vec>=0.1.6`, `fastembed>=0.4` |

## Архитектурные решения

- **Single-thread executor** (`ThreadPoolExecutor max_workers=1`): весь RAG (коннект sqlite + embedder + index/search/backfill) живёт в ОДНОМ потоке. SQLite не thread-safe → один владелец коннекта = нет race. `rag.run(loop, method, *args)` выполняет `get_rag().<method>` ВНУТРИ executor (иначе коннект привязался бы к loop-потоку → `check_same_thread` crash).
- **Async без блокировки**: `search_memory` (async тул) и индексация гоняются через `run_in_executor` → event loop не замирает на embed (~15-40мс) + brute-force скане.
- **Гибрид + RRF**: vec KNN (pool=limit×4) + FTS5 BM25 (pool=limit×4) → слияние RRF (k=60) → top-limit. Покрывает и смысл (вектор), и точные слова/имена (BM25).
- **Schema-version migration** (`PRAGMA user_version` vs `SCHEMA_VERSION`): при изменении схемы или alpha-формата sqlite-vec — дроп+ребилд индекса из `messages.db` (индекс производный, дроп безопасен). `messages.db` (source of truth) не трогается.
- **Изоляция/фильтры**: chat_id ВСЕГДА (+ PARTITION KEY шардит индекс), role опционально (в vec0 И в FTS5).
- **Модель**: `paraphrase-multilingual-MiniLM-L12-v2` (384 dims, ~220MB, без torch). Планировался mE5-small, но в FastEmbed его нет по имени (ONNX не по ожидаемому пути в HF). MiniLM — нативно в FastEmbed, те же 384 dims, русский ок. Архитектура не изменилась.

## Тесты

- **Прогнаны (без модели):** `test_rrf_merge`, `test_schema_migration_drops_old` — PASS. Логику SQL/схемы/идемпотентности/изоляции/role-фильтра дополнительно проверил ad-hoc прогоном со stubbed embedder на реальном sqlite-vec 0.1.9 — всё ОК.
- **Smoke-DDL:** sqlite-vec 0.1.9 — vec0 PARTITION KEY + metadata, FTS5 BM25, KNN с chat_id фильтром подтверждены.
- **НЕ прогнаны (требуют модель):** 8 model-dependent тестов — huggingface.co недоступен из dev-среды (proxy+mirror фейлят на HEAD/SSL). **Прогнать на VPS после деплоя** (там Xray → HF доступен). Модель скачается при первом старте бота.

## Codex review

3 раунда (`docs/tasks/rag-memory/codex-review-impl.md`):
- **Round 1:** 3 blocking — `get_rag()` вызывался в loop-потоке до `run_in_executor` (SQLite thread affinity) ×3 места. Fixed (helper `rag.run`).
- **Round 2:** 1 blocking — FTS5 миграция (`CREATE IF NOT EXISTS` не обновит старую таблицу без `role`). Fixed (schema-version drop+rebuild).
- **Round 3:** **APPROVED.**

## Breaking changes
Нет. `messages.db` schema не тронута (только +callback в MessageLog). `vec.db` — новый файл.

## Known issues / TODO
- ⚠️ **Деплой-зависимость:** при первом старте на VPS бот скачает MiniLM (~220MB) с HuggingFace. Убедиться что HF доступен через прокси на VPS, иначе RAG не поднимется (но бот стартует — embedder lazy, индексация просто отвалится в лог, `search_memory` упадёт на fallback LIKE).
- ⚠️ **Прогнать model-dependent тесты на VPS** после деплоя.
- Длинные сообщения >512 токенов обрезаются моделью (chunking не делаю — реплики короткие). Если всплывут длинные — добавить дробление.
