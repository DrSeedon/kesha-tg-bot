# RAG-память для Kesha — implementation plan

**Стек (из research.md):** sqlite-vec + multilingual-e5-small (FastEmbed int8 ONNX) + гибрид FTS5+вектор (RRF) + MCP tool `search_memory`.

**Принцип:** новый модуль `rag.py` инкапсулирует ВСЁ (embedder + индекс + поиск + backfill). Остальные файлы трогаем минимально — один хук в `message_log.py`, один тул в `kesha_tools.py`, инициализация в `bot.py`. `chat_state.py` / `response_stream.py` НЕ трогаем (логирование уже идёт через `MessageLog`).

---

## Файлы и изменения

### 1. `requirements.txt` — +2 зависимости
```
sqlite-vec>=0.1.6
fastembed>=0.4
```
- `sqlite-vec>=0.1.6` — метадата-колонки появились в 0.1.6 (нужны для фильтра по chat_id/role).
- `fastembed` тащит `onnxruntime` (не torch). `multilingual-e5-small` поддерживается из коробки.
- **Shared-файл** — править через оркестратора (но это worktree, конфликта нет; коммитим в своей ветке).

### 2. `rag.py` — НОВЫЙ модуль (ядро, ~220 строк)

Отдельная БД `storage/vec.db` (НЕ мешаем с messages.db — vec-таблица alpha-формата, дроп/ребилд без риска для source of truth).

### Concurrency-модель (РЕШЕНО — Codex blocking #1, #2)

**Проблема:** `search_memory` — `async def` MCP-тул. Синхронный embed (~15-40мс) + brute-force скан внутри event loop **заморозят бота для всех чатов**. А `RagMemory` с открытым sqlite-коннектом, шаренный между loop-потоком и `to_thread`-воркером → SQLite не thread-safe → corruption/`ProgrammingError`.

**Решение — единый выделенный поток для ВСЕХ операций RAG (один writer-thread):**
- `RagMemory` работает в ОДНОМ потоке через `concurrent.futures.ThreadPoolExecutor(max_workers=1)`. Коннект к sqlite, embedder, индексация, поиск — всё внутри этого executor'а. Один поток = нет race на коннекте, нет нужды в `check_same_thread=False` хаках и локах.
- **Индексация** (фоновый воркер): `await loop.run_in_executor(rag_executor, rag.index_message, ...)`.
- **Поиск** (из async тула): `await loop.run_in_executor(rag_executor, rag.search, chat_id, query, ...)`.
- Embedder (FastEmbed) тоже живёт в этом потоке — `max_workers=1` сериализует доступ, щадит 2 CPU (один embed за раз, не параллелим).
- `rag_executor` создаётся в `bot.py` `main()`, передаётся и в воркер индексации, и в тул (через модульную переменную в `rag.py`: `rag.set_executor(ex)` / `rag.run(fn, *args)` хелпер).

Это убирает blocking #1 (event loop не блокируется — работа в executor) и #2 (один поток владеет коннектом — нет шаринга между потоками).

### Схема БД (РЕШЕНО — Codex blocking #3, #4)

**FTS5 `content=''` — ОШИБКА в черновике (Codex #4).** Contentless FTS5 не хранит текст → BM25 MATCH работает, но `chat_id`-фильтр в одном запросе неудобен, а content для возврата всё равно нужен из messages.db. Упрощаем: **обычная FTS5-таблица хранит свой content + chat_id** (дублирование текста неизбежно для BM25, это десятки MB — приемлемо, research.md §5).

**Рабочий DDL (проверен локально: sqlite 3.46.1, FTS5 yes, enable_load_extension yes):**
```sql
-- vec0: вектор + метадата-колонки (фильтр ДО дистанции)
CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
    message_id INTEGER PRIMARY KEY,
    chat_id INTEGER PARTITION KEY,   -- шардинг по юзеру: ускоряет brute-force
    role TEXT,
    embedding FLOAT[384]
);
-- FTS5: BM25 keyword. Хранит content + chat_id (обычная, НЕ contentless)
CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
    content,
    chat_id UNINDEXED,
    message_id UNINDEXED
);
-- идемпотентность backfill/restart
CREATE TABLE IF NOT EXISTS indexed (message_id INTEGER PRIMARY KEY);
```
- vec0 metadata-колонки и `PARTITION KEY` требуют **sqlite-vec >= 0.1.6** — зафиксировано в requirements.
- FTS5 chat_id фильтр: `WHERE fts_messages MATCH ? AND chat_id = ?` (chat_id как UNINDEXED-колонка работает в WHERE).
- **DDL обязателен к проверке прогоном** на старте Phase 3 (мини-скрипт: create + insert + knn + fts match) ДО написания остального. Если sqlite-vec не грузится / PARTITION KEY не поддержан в установленной версии — фолбэк на обычную metadata-колонку без partition (план B, отметить).

**Структура модуля:**

```python
"""RAG semantic memory — FastEmbed (mE5-small) + sqlite-vec hybrid search."""
import logging, sqlite3
from pathlib import Path
import sqlite_vec

logger = logging.getLogger("kesha.rag")
DB_PATH = Path("./storage/vec.db")
MSG_DB_PATH = Path("./storage/messages.db")
MODEL_NAME = "intfloat/multilingual-e5-small"
DIM = 384
RRF_K = 60

class RagMemory:
    """ВСЕ методы вызываются ТОЛЬКО из rag_executor (single thread). Коннект и embedder
    привязаны к этому потоку — не дёргать из других потоков."""
    def __init__(self, path=DB_PATH):
        # connect (check_same_thread=True — мы всегда в одном потоке), WAL
        # enable_load_extension; sqlite_vec.load(conn); enable_load_extension(False)
        # ATTACH messages.db AS msg (read-only) для джойна content/timestamp
        # create schema (DDL выше)
        # _embedder = None  (lazy)
    def _embed(self, texts, is_query) -> list[list[float]]:
        # lazy-init FastEmbed TextEmbedding(MODEL_NAME)
        # E5 prefix: "query: " / "passage: "
    def index_message(self, message_id, chat_id, role, content) -> None:
        # skip empty/whitespace; skip role=='system'; skip if message_id in indexed
        # embed(passage) -> INSERT vec_messages + fts_messages + indexed (одна транзакция)
    def search(self, chat_id, query, limit=5, role=None, before=None, after=None) -> list[dict]:
        # vec KNN (LIMIT limit*4, WHERE chat_id [AND role]) -> ranked message_ids
        # FTS5 BM25 (LIMIT limit*4, MATCH + chat_id) -> ranked message_ids
        # _rrf merge -> top `limit`; JOIN msg.messages по message_id -> content+timestamp
        # before/after фильтр по timestamp на джойне; return list[dict]
    def backfill(self, batch_size=64) -> int:
        # SELECT из msg.messages WHERE id NOT IN indexed AND role!='system' AND trim(content)!=''
        # батчами embed+insert. идемпотентно по indexed PK. return count.

# --- module-level: executor wiring (используется и воркером, и тулом) ---
_db = None
_executor = None        # ThreadPoolExecutor(max_workers=1), ставится из bot.py
def set_executor(ex): ...
def get_rag() -> RagMemory: ...   # singleton, СОЗДАётся внутри executor-потока при первом run
async def run(loop, fn_name, *args):   # await loop.run_in_executor(_executor, getattr(get_rag(), fn_name), *args)
```

**Ключевые детали реализации:**

- **E5-префиксы зашиты в `_embed`** — passage:/query:. Забыть = тихая деградация (research.md §3).
- **Candidate pool** vec и FTS по `limit*4`, RRF сужает до `limit`. Pool нужен чтобы RRF имел что сливать.
- **chat_id-фильтр ВСЕГДА** (изоляция 2 юзеров + pre-filter ускоряет скан; PARTITION KEY шардит индекс).
- **content/timestamp** — НЕ дублируем в vec0; берём из `msg.messages` через ATTACH (read-only) на джойне. FTS5 хранит свою копию content (нужна для BM25, неизбежно).
- **WAL** на vec.db.
- **RagMemory создаётся лениво ВНУТРИ executor-потока** (первый `run()` инициализирует singleton в нужном потоке) — иначе коннект привяжется к loop-потоку. Критично для blocking #2.
- **before/after** — ISO-строки, фильтр по timestamp из messages.db на джойне.

**RRF-функция (без модели, research.md §6):**
```python
def _rrf(vec_ranked, fts_ranked, k=RRF_K):
    scores = {}
    for rank, mid in enumerate(vec_ranked): scores[mid] = scores.get(mid,0) + 1/(k+rank)
    for rank, mid in enumerate(fts_ranked): scores[mid] = scores.get(mid,0) + 1/(k+rank)
    return sorted(scores, key=scores.get, reverse=True)
```

### 3. `message_log.py` — хук инкрементальной индексации (минимальная правка)

Сейчас `log_user`/`log_assistant` возвращают `None`. Меняем:
- `log_user`/`log_assistant` → возвращают `int` (lastrowid вставленной строки).
- После insert → **fire-and-forget** постановка в очередь индексации (НЕ блокируем ответ юзеру — research.md §6).

**Как не блокировать:** `MessageLog` получает callback `on_message(msg_id, chat_id, role, content)`, который `bot.py` ставит как `lambda ...: rag_queue.put_nowait(...)`. Фоновый asyncio-воркер (в bot.py) разбирает очередь и зовёт `rag.index_message` (embed ~25мс — в фоне, юзер не ждёт).

Правка в `message_log.py`:
```python
class MessageLog:
    def __init__(self, ..., on_message=None):
        self._on_message = on_message   # callable(msg_id, chat_id, role, content) | None
    def log_user(self, chat_id, content, msg_id=0) -> int:
        cur = self.conn.execute("INSERT ... RETURNING id", ...)  # or lastrowid
        rid = cur.fetchone()[0]
        if self._on_message: self._on_message(rid, chat_id, "user", content)
        return rid
    # log_assistant аналогично
```
`get_db()` остаётся singleton; добавим `set_on_message(cb)` для поздней привязки из bot.py (callback недоступен на момент первого `get_db()`).

**Edge:** callback synchronous и НЕ должен бросать — оборачиваем вызов в try/except внутри log_*, лог + продолжаем (fail loud в лог, но не рушим логирование).

### 4. `kesha_tools.py` — MCP tool `search_memory`

После `update_reminder` (строка ~273), по паттерну `list_reminders`:
```python
@tool("search_memory",
      "Semantic search across the ENTIRE dialog history (survives context compaction). "
      "Use when user references past topics ('помнишь', 'что я говорил про...') or after compaction "
      "when you need details no longer in context. query: what to find (natural language). "
      "role: filter 'user'/'assistant' (optional). limit: max results (default 5).",
      {"query": str, "limit": int, "role": str})
async def search_memory(args):
    chat_id = _require_chat()
    if isinstance(chat_id, dict): return chat_id
    query = (args.get("query") or "").strip()
    if not query:
        return {"content": [{"type": "text", "text": "query is required"}], "is_error": True}
    limit = args.get("limit") or 5
    role = args.get("role") or None
    try:
        import asyncio, rag
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(rag._executor, rag.get_rag().search, chat_id, query, limit, role)
        # (search signature: search(chat_id, query, limit, role) — позиционно через executor)
    except Exception as e:
        # graceful degradation: fallback to LIKE-search (research.md §6 edge case)
        logger.error(f"search_memory failed: {e}", exc_info=True)
        from message_log import get_db
        rows = [dict(r) for r in get_db().search(chat_id, query, limit)]
    if not rows:
        return {"content": [{"type": "text", "text": "No matches in history"}]}
    lines = [f"[{r['timestamp']} | {r['role']}] {r['content']}" for r in rows]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}
```
+ добавить `search_memory` в `tools=[...]` (строка 473-476).

**Формат результата компактный** (research.md §4): `[timestamp | role] content`. top-5 ≈ 300-600 токенов.

### 5. `bot.py` — wiring (executor + очередь + воркер + backfill)

- **import:** `import rag`, `from concurrent.futures import ThreadPoolExecutor`.
- **Executor + очередь + воркер** в `main()` (один поток на весь RAG — см. Concurrency-модель):
  ```python
  rag_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag")
  rag.set_executor(rag_executor)
  loop = asyncio.get_running_loop()
  rag_queue: asyncio.Queue = asyncio.Queue()

  async def _rag_worker():
      while True:
          mid, cid, role, content = await rag_queue.get()
          try:
              await loop.run_in_executor(rag_executor, rag.get_rag().index_message, mid, cid, role, content)
          except Exception as e:
              logger.error(f"rag index failed mid={mid}: {e}")
          finally:
              rag_queue.task_done()
  asyncio.create_task(_rag_worker())
  ```
- **Привязка callback** к message_log (log_* зовётся из async-контекста бота, но безопаснее через threadsafe):
  ```python
  from message_log import get_db as _msg_db
  _msg_db().set_on_message(
      lambda mid, cid, role, c: loop.call_soon_threadsafe(rag_queue.put_nowait, (mid, cid, role, c))
  )
  ```
  `call_soon_threadsafe` + `put_nowait` — очередь без лимита, на 2 юзера не переполнится. Индексация сериализована в executor (1 embed за раз — щадит 2 CPU).
- **Backfill при старте** (фоном, разово, в том же executor):
  ```python
  asyncio.create_task(loop.run_in_executor(rag_executor, rag.get_rag().backfill))
  ```
  Не блокирует старт; идемпотентно (таблица `indexed`). Первый `get_rag()` инициализирует singleton в executor-потоке — коннект и embedder привяжутся к нужному потоку.
- **Shutdown:** `rag_executor.shutdown(wait=False)` в `finally` блока `main()` (рядом с `registry.shutdown()`).

### 6. `system_prompt.txt` — подсказка (1 абзац)

Добавить блок:
> У тебя есть `search_memory` — семантический поиск по ВСЕЙ истории диалога (переживает compact/сброс контекста). Зови его когда юзер ссылается на прошлое ("помнишь...", "что я говорил про...", "мы обсуждали"), или когда после compact тебе нужны детали которых уже нет в контексте. Не зови на каждое сообщение — только когда реально нужна память.

---

## Что НЕ трогаем

- `messages.db` schema — **никаких ALTER**. Индексация идёт в отдельную `vec.db`, идемпотентность через таблицу `indexed` в vec.db (не флаг в messages). Это чище: messages.db остаётся чистым логом.
- `chat_state.py`, `response_stream.py` — логирование уже идёт через `MessageLog`, хук внутри него → эти файлы не трогаем.
- `reminders.py`, стриминг, ChatState, compact — вне scope.

## Edge cases (закрываем в коде)

| Кейс | Обработка |
|------|-----------|
| Пустой/whitespace content | skip в `index_message` |
| `role='system'` | не индексируем |
| Дубли (повторный backfill/restart) | таблица `indexed` (message_id PK) |
| Embedder не загрузился / OOM | `search_memory` → fallback на LIKE-search; индексация → лог ошибки, не рушит бота |
| sqlite-vec формат сломался на апгрейде | дроп vec.db + backfill (данные в messages.db целы) |
| Длинное сообщение > 512 токенов | mE5 обрежет сам (на старте ок; дробление — не сейчас) |
| Очередь индексации растёт (бурст) | asyncio.Queue без лимита; на 2 юзера не проблема. Воркер последовательный (1 embed за раз — щадит 2 CPU) |
| Поиск пустой query | tool возвращает is_error |

## Тестирование (Phase 3)

- `tests/test_rag.py`: index→search round-trip; idempotency backfill; chat_id изоляция; empty/system skip; RRF слияние; fallback при сломанном embedder (мок).
- Прогон: `UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q`.
- Smoke: `python -c "import bot"` (как в PROCESS RULES).
- Ручной: проиндексировать 2-3 сообщения, поискать на русском, проверить что находит по смыслу (не только по словам).

## Порядок реализации

0. **Smoke-DDL первым** (Codex #3): мини-скрипт — `sqlite_vec.load` + create vec0/fts5 (DDL выше) + insert + knn + fts match. Подтвердить что установленная sqlite-vec поддерживает metadata-колонки и PARTITION KEY. Не поддерживает → план B (metadata без partition). БЕЗ этого шага не писать ядро.
1. `requirements.txt` + `uv sync` (проверить что fastembed/sqlite-vec ставятся).
2. `rag.py` (ядро, single-thread executor-модель) + юнит-тесты (TDD на index/search/RRF — data layer, CLAUDE.md TDD-зона). Тесты вызывают методы напрямую (без executor — он только для прода-async).
3. `message_log.py` хук (+тест что callback зовётся, msg_id возвращается).
4. `kesha_tools.py` tool + регистрация.
5. `bot.py` wiring (queue, worker, backfill).
6. `system_prompt.txt`.
7. Прогон тестов + smoke + Codex review diff.

## Оценка ресурсов (из research.md, подтверждаю)

- RAM: +~120-150MB (модель int8, lazy при первом embed).
- Диск: ~200-250MB на 100K (vec float32 ~160MB + FTS ~десятки MB).
- Поиск: ~30-80мс end-to-end. Индексация: ~25мс/сообщение в фоне.
- Backfill 100K: несколько минут разово, не блокирует старт.
