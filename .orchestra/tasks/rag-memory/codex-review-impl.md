---
slug: impl-review
model: gpt-5.5
---

## Summary
Реализация в целом следует выбранной простой архитектуре: отдельный `vec.db`, FTS5, RRF и fire-and-forget индексация через очередь. Callback из `message_log` выглядит thread-safe: он не трогает asyncio-queue напрямую из чужого потока, а делает `loop.call_soon_threadsafe`. Backfill идемпотентен по таблице `indexed` и из-за одного executor-потока не конфликтует с инкрементальной индексацией. Но есть один критичный регресс: `get_rag()` фактически вызывается в event loop thread до `run_in_executor`, поэтому SQLite-коннект создаётся не в RAG-потоке и вся RAG-память будет падать на `check_same_thread`.

## Findings

- blocking: `bot.py:178` — `_rag.get_rag().index_message` вычисляется до вызова `run_in_executor`, то есть singleton `RagMemory` и sqlite-коннект создаются в event loop thread, а сам метод потом выполняется в executor thread → `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`; заменить на wrapper, выполняющий `get_rag()` внутри executor: `await loop.run_in_executor(_rag_executor, lambda: _rag.get_rag().index_message(mid, cid, role, content))` или добавить в `rag.py` helper `run(fn_name, *args)`.

- blocking: `bot.py:190` — backfill стартует с той же ошибкой: `_rag.get_rag().backfill` создаёт singleton в loop thread ещё до передачи callable в executor, после чего `backfill()` использует этот коннект в RAG-потоке → первый старт RAG стабильно ломается; фикс тот же: `loop.run_in_executor(_rag_executor, lambda: _rag.get_rag().backfill())` / общий helper.

- blocking: `kesha_tools.py:296` — `search_memory` тоже вызывает `rag.get_rag()` в event loop thread перед `run_in_executor`; если singleton ещё не создан, поиск сам привяжет SQLite к loop-потоку, а если уже создан в executor — упадёт прямо в loop на `check_same_thread`; передавать в executor только thunk `lambda: rag.get_rag().search(chat_id, query, limit, role)`.

- suggestion: `kesha_tools.py:290` — `limit` берётся из tool args без нормализации; отрицательный `limit` попадёт в `rag.py:142` и превратит SQLite `LIMIT ?` в фактически неограниченный candidate scan, а fallback `LIKE` тоже получит отрицательный limit; зажать в безопасный диапазон, например `limit = min(max(int(limit), 1), 20)`.

- suggestion: `rag.py:111` — FTS-ветка не фильтруется по `role`, потому что в `fts_messages` нет role; при `role='assistant'` RRF pool может быть забит user-документами, которые потом отфильтруются на join, и результат вернёт меньше `limit` даже при наличии assistant-совпадений; добавить `role UNINDEXED` в FTS и фильтровать в `_fts_search`, либо сильно overfetch/refill при role-фильтре.

- suggestion: `rag.py:173` — `backfill()` сначала загружает все неиндексированные сообщения в память, а потом батчит только embedding/insert; для заявленных 100K сообщений на VPS 3.5GB это может дать лишний memory spike на длинной истории; читать порциями `LIMIT ?` с повторным `WHERE id NOT IN indexed` после каждого commit, сохраняя текущую идемпотентность.

## Verdict

Требует доработки: архитектура правильная, но текущий wiring нарушает главное инвариантное требование “RagMemory создаётся и живёт только в одном executor-потоке”. После фикса ленивой инициализации внутри executor реализация выглядит рабочей для масштаба 2 пользователя / до 100K сообщений; остальные замечания не блокируют запуск, но улучшат предсказуемость и защиту от edge cases.

## Round 2 — re-review

### Статус предыдущих пунктов

- FIXED: `bot.py:178` — worker теперь вызывает `await _rag.run(loop, "index_message", ...)`; `get_rag()` выполняется внутри executor-thunk в `rag.py:234-235`, а не в event loop thread.
- FIXED: `bot.py:190` — backfill теперь стартует как `asyncio.ensure_future(_rag.run(loop, "backfill"))`; singleton создаётся в RAG executor.
- FIXED: `kesha_tools.py:295` — `search_memory` теперь делает `await rag.run(loop, "search", ...)`, без прямого `rag.get_rag()` из loop-потока.
- FIXED: `kesha_tools.py:290` — `limit` зажат в диапазон `1..20` для числовых/отсутствующих значений.
- FIXED для чистой базы: `rag.py:58-60`, `rag.py:111-120` — в `fts_messages` добавлен `role UNINDEXED`, `_fts_search` фильтрует по `role`.
- FIXED: `rag.py:175-207` — backfill читает историю чанками через повторный `WHERE ... NOT IN indexed ... LIMIT ?`, не загружая все сообщения в память.

### Новые замечания

- blocking: `rag.py:57-60`, `rag.py:92`, `rag.py:199` — нет миграции уже созданной `fts_messages` без колонки `role`. `CREATE VIRTUAL TABLE IF NOT EXISTS` не изменит существующую FTS5-таблицу из Round 1; после этого `INSERT INTO fts_messages(content, chat_id, role, message_id)` и role-фильтр в `_fts_search` будут падать на существующем `storage/vec.db`. Нужен явный rebuild/drop+recreate FTS при несовпадении схемы или bump/reset `vec.db`.
- suggestion: `kesha_tools.py:290` — `int(args.get("limit") or 5)` находится до `try`; строка вроде `"abc"` уронит tool без fallback. Если tool args приходят строго типизированными, это не критично, но защитный парсинг лучше держать рядом с clamp.

### Verdict

Требует доработки: старые threading-blocker исправлены, но in-place обновление с уже существующим `vec.db` может сломать индексацию и поиск из-за отсутствующей миграции FTS-схемы. Тесты не запускал по условию.

## Round 3 — re-review

### Статус Round 2

- FIXED: `rag.py:51-75` — схема теперь версионируется через `PRAGMA user_version`; при `ver != SCHEMA_VERSION` производные таблицы `vec_messages`, `fts_messages`, `indexed` дропаются и пересоздаются. Для существующего `vec.db` из Round 1/2 с `user_version=0` это убирает старую FTS5-схему без `role`, а backfill восстановит индекс из `messages.db`.
- FIXED: `kesha_tools.py:290-293` — `int(limit)` теперь находится внутри `try`, некорректный тип/строка падают в безопасный default `5`, а не роняют tool.

### Новые замечания

Нет blocking/suggestion замечаний по миграции. Дроп не будет происходить на каждом старте: после первого пересоздания выставляется `user_version=1`, а при следующем запуске ветка `ver != SCHEMA_VERSION` не сработает. Потеря данных ожидаемо ограничена `vec.db`, который здесь является производным индексом; исходная история остаётся в `messages.db`.

### Verdict

APPROVED. Тесты не запускал по условию.

Финальная сверка текущего diff: вердикт без изменений — APPROVED; замечаний сверх уже перечисленного нет.
