# RAG-память для Kesha TG Bot — research report

**Задача:** семантический поиск по ВСЕЙ истории диалогов (`messages.db`), чтобы после `compact` контекст не терялся.
**Дата:** 2026-06-26
**Масштаб:** 2 юзера, 50-200 сообщений/день, рост до 10K-100K total.
**Железо:** VPS 3.5GB RAM свободно, 2 CPU, нет GPU, РФ (прокси для внешних API).

---

## TL;DR — рекомендация

**Стек: `sqlite-vec` + `multilingual-e5-small` (через FastEmbed, int8 ONNX) + гибридный поиск (FTS5 + вектор, RRF) + MCP tool `search_memory`.**

| Компонент | Выбор | Почему |
|-----------|-------|--------|
| Векторное хранилище | **sqlite-vec** | Тот же стек, что `messages.db` (SQLite WAL). Ноль новых серверов, один файл, embedded. На 100K векторах brute-force на CPU — это **единицы-десятки мс**. |
| Embedding модель | **multilingual-e5-small** (384 dims) | 118M параметров, ~120MB RAM в int8 ONNX. Хороший русский (MIRACL ru в составе mE5). На 2 CPU ~15-40 мс/embedding. |
| Runtime модели | **FastEmbed** (`qdrant/fastembed`) | ONNX-only, БЕЗ PyTorch (экономия ~1.5-2GB RAM и сотен MB диска). Ровно то, что нужно для VPS. |
| Поиск | **Гибрид FTS5 (BM25) + вектор, fusion через RRF** | Русский язык + аббревиатуры/имена → keyword ловит точные слова, вектор ловит смысл. RRF (k=60) объединяет. |
| Архитектура | **MCP tool `search_memory`** (Вариант B), позже опционально auto-inject | Кеша сам решает когда искать → не жжём контекст на каждом запросе. |

**Расход на VPS:** модель ~120-150MB RAM (постоянно загружена), индекс на 100K сообщений ~60-150MB на диске, поиск ~30-80мс end-to-end. Влезает с огромным запасом в 3.5GB.

---

## 1. Текущая архитектура (что есть сейчас)

`message_log.py` уже логирует ВСЁ:

```python
# storage/messages.db (SQLite WAL)
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,           -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    message_id INTEGER,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX idx_messages_chat_ts ON messages(chat_id, timestamp);
```

Уже есть `MessageLog.search()` — но это `LIKE '%query%'`, тупой substring без семантики и без ранжирования.

**Паттерн MCP-тулов** (из `kesha_tools.py`): `@tool("name", "desc", {schema})` + `create_sdk_mcp_server(...)`. Активный `chat_id` прокидывается через `set_active_chat()` перед каждым `_ask`. Новый `search_memory` встроится по этому же паттерну — добавить в `tools=[...]` список на строке 473.

**Реминдеры** (`reminders.py`) — отдельная `storage/reminders.db`, свой sqlite-коннект с WAL. Тот же паттерн ляжет на RAG.

### Файлы, которые затронет интеграция

| Файл | Что меняется |
|------|--------------|
| `message_log.py` | +метод записи embedding при логировании (или отдельный модуль `rag.py`) |
| `kesha_tools.py` | +`@tool("search_memory", ...)`, +в список `tools=[...]` |
| **новый** `rag.py` | embedder (FastEmbed) + sqlite-vec индекс + hybrid search + RRF + backfill |
| `requirements.txt` | +`sqlite-vec`, +`fastembed` |
| `bot.py` | инициализация embedder при старте (lazy), backfill-хук |
| `system_prompt.txt` | подсказка Кеше когда звать `search_memory` |

---

## 2. Векторные БД (embedded, без сервера)

### Сравнение

| | **sqlite-vec** | ChromaDB | LanceDB | zvec |
|-|----------------|----------|---------|------|
| Тип | SQLite extension | in-memory + persist | disk (Lance/Arrow, mmap) | disk ANN (Rust/C++) |
| Зависимости | **~0** (pip wheel, грузится в наш sqlite) | chromadb + onnx/torch | lancedb + pyarrow (Rust) | сборка/биндинги |
| RAM | минимум (часть нашего sqlite-процесса) | весь датасет в RAM | mmap, диск > RAM ок | низкий |
| Индекс | **brute-force only** (нет ANN пока) | HNSW | IVF-PQ (ANN) | дисковый ANN |
| Гибрид (vec+FTS) | **да, нативно через FTS5+RRF** | частично | да (BM25+vector) | да |
| Один файл | **да** (наш же `.db`) | каталог | каталог | каталог |
| Метадата-фильтр | **да** (v0.1.6+: WHERE по chat_id и т.д.) | да | да | да |
| Зрелость | alpha (pre-v1, возможны breaking changes формата) | стабильна | стабильна (Python — самый зрелый клиент) | растущая |

### Почему sqlite-vec, а не ChromaDB/LanceDB/zvec

1. **Один стек.** У нас уже SQLite WAL (`messages.db`, `reminders.db`). sqlite-vec — это `pip install sqlite-vec` + `sqlite_vec.load(conn)` на существующем коннекте. Никаких новых процессов, демонов, каталогов, форматов. Это прямо в духе проекта ("simple, embedded, minimal dependencies").

2. **Brute-force — это НЕ проблема на нашем масштабе.** sqlite-vec пока без ANN-индекса (полный скан таблицы). На 100K векторов × 384 float32 это ~150MB данных, скан которых на CPU — **единицы-десятки мс** (особенно с pre-filter по `chat_id`: у нас 2 юзера, фильтр режет датасет вдвое до расчёта дистанции). ANN нужен от миллионов векторов — мы туда не дойдём.

3. **ChromaDB** тащит оннх/часто torch + держит датасет в RAM. Для 2 юзеров это из пушки по воробьям, +лишние сотни MB RAM и зависимостей.

4. **LanceDB** силён когда датасет > RAM (миллионы-миллиарды). У нас 100K — это мегабайты. Оверкилл + Rust-сборка + ещё один каталог-формат.

5. **zvec** (Alibaba) — дисковый ANN, заточен под большой масштаб и гибрид. Опять же — для нашего объёма ANN не нужен, а зависимость тяжелее sqlite-vec.

**Риск sqlite-vec:** pre-v1 alpha, возможны breaking changes формата хранения. Митигация: embeddings можно **перегенерить из `messages.db` за минуты** (backfill), индекс — производная, не source of truth. Если формат сломается на апгрейде — дропнули vec-таблицу, перестроили.

### Как хранить (sqlite-vec)

```sql
-- vec0 virtual table: вектор + метадата-колонки для фильтрации
CREATE VIRTUAL TABLE vec_messages USING vec0(
    message_id INTEGER PRIMARY KEY,   -- = messages.id
    chat_id INTEGER,                  -- метадата: фильтр перед расчётом дистанции
    role TEXT,                        -- метадата: можно искать только по user/assistant
    embedding float[384]              -- mE5-small
);
```

KNN-запрос с pre-filter:
```sql
SELECT message_id, distance
FROM vec_messages
WHERE chat_id = :chat_id          -- метадата-фильтр ДО дистанции (быстрее)
  AND embedding MATCH :query_vec
ORDER BY distance LIMIT 10;
```

**Квантизация (опционально, если захотим ужать):** sqlite-vec умеет `bit[384]` (binary, 32× меньше: 48 байт/вектор) и `int8`. Но на 100K float32 индекс ~60MB — квантизация не нужна, оставляем float32 ради качества. Это рычаг на будущее, не сейчас.

---

## 3. Embedding модели (русский + английский)

### Сравнение для CPU + ограниченный RAM

| Модель | Params | Dims | RAM (fp32 / int8) | CPU latency¹ | Русский | Вердикт |
|--------|--------|------|-------------------|--------------|---------|---------|
| **multilingual-e5-small** | 118M | 384 | ~470MB / **~120MB** | ~15-40 мс | хороший (MIRACL ru) | ✅ **выбор** |
| multilingual-e5-base | 278M | 768 | ~1.1GB / ~280MB | ~40-90 мс | лучше | запас по качеству |
| multilingual-e5-large | 560M | 1024 | ~2.2GB / ~560MB | ~100-250 мс | отличный (MIRACL ru 66.5) | тяжеловат для 2 CPU |
| BAAI/bge-m3 | 568M | 1024 | ~2.2GB / ~570MB | ~100-300 мс | отличный, dense+sparse | оверкилл |
| FRIDA (ai-forever) | 823M (T5) | 1536 | ~3.2GB | медленно на CPU | топ ruMTEB | слишком жирный для VPS |
| OpenAI text-embedding-3-small | API | 1536→512 | 0 (API) | сеть+прокси | MIRACL 44.0 | см. ниже |

¹ latency на 2 CPU без GPU, batch=1, короткий текст. Грубая оценка; точные числа надо мерить на целевом железе (зависит от AVX2/AVX-512 VNNI).

### Почему multilingual-e5-small

- **RAM:** в int8 ONNX через FastEmbed ~120-150MB постоянно — копейки от 3.5GB.
- **Качество русского:** mE5 тренировался на MIRACL (16 языков, включая русский). Для коротких чат-сообщений (наш кейс — реплики, не статьи) small достаточно. ruMTEB-топы (FRIDA, GigaEmbeddings, USER) сильнее, но это 800M+ модели — на 2 CPU будут тормозить, а выигрыш на коротких репликах непропорционален цене.
- **384 dims** — маленький вектор → быстрый brute-force скан + компактный индекс.
- **Лимит 512 токенов** — наши сообщения почти всегда короче. Длинные (редкие) обрежутся; см. chunking ниже.

### FastEmbed vs sentence-transformers

**FastEmbed** (`qdrant/fastembed`):
- ONNX runtime, **без PyTorch** → экономия ~1.5-2GB установки и RAM.
- Заточен под низкий RAM на CPU, генераторы для батчей.
- `multilingual-e5-small` поддерживается из коробки (или регистрируется как custom с int8 ONNX).
- **Это правильный выбор для нашего VPS.**

sentence-transformers тащит torch (жирно), но даёт больше тюнинга (OpenVINO int8, optimum). Нам не нужно.

### Префиксы E5 (ВАЖНО — иначе качество просядет)

Модели E5 требуют префиксы:
- Документы (наши сообщения в индексе): `"passage: <текст>"`
- Запрос (что ищет Кеша): `"query: <текст>"`

Забыть префикс = тихая деградация retrieval. Зашить в код embed-функции, не полагаться на вызывающего.

### Вариант с API (OpenAI text-embedding-3-small)

- $0.02 / 1M токенов. На 100K сообщений × ~50 токенов = 5M токенов = **$0.10 разово** на backfill. Инкремент копейки.
- 1536 dims (можно ужать до 512 через Matryoshka).
- **Минусы для нас:**
  - РФ → нужен прокси (Ёжик `127.0.0.1:10809`) на КАЖДЫЙ embedding, включая поиск в реальном времени → латентность сети + точка отказа.
  - Зависимость от внешнего сервиса для базовой функции бота.
  - Утечка всей переписки в OpenAI (приватный ассистент — нежелательно).
- **Anthropic embeddings:** НЕТ. Anthropic не предоставляет embedding API — рекомендует Voyage AI. Не вариант для self-contained бота.
- **Вердикт:** API только если local-модель не влезет. Она влезает → локально, приватно, без сети.

---

## 4. Архитектура интеграции

### Варианты

- **A. Auto-inject** — каждый запрос → поиск → top-K в контекст. Минус: жжёт контекст и CPU на КАЖДОМ сообщении, даже когда память не нужна ("привет", "ок"). Шум в контексте.
- **B. MCP tool `search_memory`** — Кеша сам решает когда искать (как `list_reminders`). Плюс: ноль оверхеда когда не нужно, Кеша формулирует осмысленный запрос. Минус: модель должна догадаться позвать тул.
- **C. Оба** — auto-inject лёгкого контекста + tool для глубокого поиска.

### Рекомендация: B сейчас, опционально C позже

**Старт — Вариант B (MCP tool).** В духе детерминизма проекта и экономии контекста:
- Тул `search_memory(query, limit=5, role=None, before=None, after=None)`.
- Кеша зовёт когда юзер ссылается на прошлое ("помнишь, мы обсуждали...", "что я говорил про...").
- В `system_prompt.txt` — чёткая инструкция: при отсылке к прошлому/после compact зови `search_memory`.

**Почему не auto-inject сразу:** на 2 юзера и коротких репликах auto-inject на каждом сообщении = постоянный CPU-скан + замусоривание контекста нерелевантными old-сообщениями. Сначала tool, померяем как Кеша им пользуется, потом решим про C.

### Бюджет контекста на результаты

Сообщение чата ~30-80 токенов. С обвязкой (дата, роль, разделители) ~50-120 токенов на результат:
- **top-5** ≈ 300-600 токенов
- **top-10** ≈ 600-1200 токенов

Дефолт **top-5**. Формат результата компактный:
```
[2026-05-12 14:30 | user] текст сообщения
[2026-05-12 14:31 | kesha] ответ
```

---

## 5. Расход ресурсов (итог)

### RAM
- Модель mE5-small int8 ONNX (FastEmbed): **~120-150MB**, загружена постоянно (lazy при первом поиске).
- sqlite-vec индекс: часть нашего sqlite-процесса, brute-force читает с диска/page-cache, не держит всё в RAM как Chroma.
- **Итого +~150MB к боту.** Из 3.5GB — незаметно.

### Диск (индекс)
- float32 384-dim = 384×4 = **1536 байт/вектор** + метадата ~50 байт.
- 10K сообщений ≈ **16MB**
- 100K сообщений ≈ **160MB**
- FTS5-индекс для гибрида: ещё ~1-2× от размера текста (десятки MB на 100K).
- **Итого на 100K: ~200-250MB диска.** Норм.

### CPU (на VPS, 2 core, без GPU)
- 1 embedding (mE5-small int8): **~15-40 мс**.
- Поиск: brute-force скан 100K×384 + FTS5 + RRF ≈ **~10-40 мс** (с pre-filter по chat_id — быстрее).
- **End-to-end поиск: ~30-80 мс.** Незаметно для юзера.
- Backfill 100K: 100K embeddings ≈ 100K×25мс / (батчинг) ≈ **несколько минут** разово. Батчами по 32-64.

### Стоимость
- Локальная модель: **$0** (разовая загрузка весов ~120MB через прокси при первом старте).
- API-вариант (если бы): $0.10 backfill + копейки/мес. Но отвергнут (приватность, сеть, прокси).

---

## 6. Лучшие практики

### Chunking
- **1 сообщение = 1 chunk.** Реплики короткие (< 512 токенов почти всегда) → не дробим.
- Длинные сообщения (редкие, > 512 токенов): обрезаем при embed (mE5 сам обрежет) ИЛИ дробим на ~400-токенные окна с overlap 50. Хранить `parent_message_id` чтобы собрать обратно. На старте — просто обрезка, дробление если всплывут длинные.
- **Не склеивать** user+assistant в один chunk: ищем по смыслу реплики, склейка размывает.

### Metadata filtering
- По `chat_id` — ВСЕГДА (изоляция юзеров, + ускоряет brute-force pre-filter'ом).
- По `role` — опционально (искать только свои слова или только ответы Кеши).
- По дате (`before`/`after`) — для "что я говорил на прошлой неделе". sqlite-vec метадата-колонки + WHERE.

### Reranking
- **RRF (Reciprocal Rank Fusion, k=60)** для слияния FTS5(BM25) + vector — да, нужен. Дёшево, без модели: `score = 1/(k+rank_fts) + 1/(k+rank_vec)`.
- **Cross-encoder reranker** (отдельная модель) — НЕ нужен. Это ещё одна тяжёлая модель на CPU ради маржинального прироста на 2 юзерах. RRF достаточно.

### Incremental indexing
- При каждом `log_user` / `log_assistant` → сразу embed + insert в vec-таблицу (синхронно или в фоне через очередь).
- Риск: embed добавит ~25мс к логированию. Решение: писать в `messages.db` сразу (как сейчас), а embedding — **в фоновой таске** (asyncio), не блокируя ответ юзеру. `embedded` флаг в messages, фоновый воркер добирает неэмбедженное.

### Backfill существующей messages.db
- Разовый скрипт: читаем все `messages` где нет embedding → батчами по 32-64 → embed → insert в `vec_messages`.
- ~несколько минут на 100K. Запустить один раз при деплое фичи.
- Идемпотентность: по `message_id` (PRIMARY KEY в vec0) — повторный запуск не дублирует.

### Edge cases (Edge-Case Hunter)
- **Пустой/whitespace content** → скип, не эмбедим (мусорный вектор).
- **Очень короткие ("ок", "да")** → эмбедим, но они редко всплывут в поиске — ок.
- **role='system'** (служебные) → не индексировать (не часть диалога).
- **Дубли сообщений** → message_id PK защищает.
- **Модель не загрузилась / OOM** → fail loud, лог, фолбэк на старый `LIKE`-search в `search_memory` (graceful degradation, не падаем).
- **sqlite-vec формат сломался на апгрейде** → дроп vec-таблицы + backfill из messages.db.

---

## 7. Примерный план реализации (для Phase 2)

1. **`rag.py`** — новый модуль:
   - `Embedder` (FastEmbed mE5-small, int8, lazy load, E5-префиксы зашиты).
   - `VecIndex` (sqlite-vec на отдельном `storage/vec.db` ИЛИ в `messages.db` — решить в plan).
   - `hybrid_search(chat_id, query, limit, filters)` → FTS5 + vector + RRF.
   - `index_message(msg_id, chat_id, role, content)` — инкремент.
   - `backfill()` — разовый прогон по messages.db.
2. **`message_log.py`** — хук: после insert → enqueue для фонового эмбеддинга (флаг `embedded`).
3. **`kesha_tools.py`** — `@tool("search_memory", ...)` → `rag.hybrid_search(active_chat_id, ...)`, формат результата компактный. +в `tools=[...]`.
4. **`bot.py`** — старт фонового embed-воркера (asyncio), lazy-init embedder.
5. **`requirements.txt`** — `sqlite-vec`, `fastembed`.
6. **`system_prompt.txt`** — инструкция когда звать `search_memory`.
7. **Backfill** — запустить разово на VPS после деплоя.

**Что НЕ трогать:** существующий `messages.db` schema (только +флаг `embedded` ALTER), `reminders.py`, стриминг, ChatState.

---

## Источники

**Векторные БД:**
- [Embedded Vector Databases comparison — chromem-go vs sqlite-vec vs LanceDB](https://shaharia.com/blog/choosing-embeddable-vector-database-go-application/)
- [LanceDB vs ChromaDB — Disk-Based vs In-Memory](https://aicoolies.com/comparisons/lancedb-vs-chromadb)
- [Vector Database Comparison 2026 (4xxi)](https://4xxi.com/articles/vector-database-comparison/)
- [sqlite-vec GitHub (asg017)](https://github.com/asg017/sqlite-vec)
- [Hybrid full-text + vector search with SQLite — Alex Garcia](https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html)
- [sqlite-vec metadata columns & filtering](https://alexgarcia.xyz/blog/2024/sqlite-vec-metadata-release/index.html)
- [sqlite-vec Binary Quantization guide](https://alexgarcia.xyz/sqlite-vec/guides/binary-quant.html)
- [Hybrid Search FTS5 + Vector + RRF](https://ceaksan.com/en/hybrid-search-fts5-vector-rrf)

**Embedding модели:**
- [Best Open-Source Embedding Models 2026 (BentoML)](https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models)
- [Multilingual E5 Technical Report (arXiv)](https://arxiv.org/pdf/2402.05672)
- [intfloat/multilingual-e5-small (HF)](https://huggingface.co/intfloat/multilingual-e5-small)
- [deepfile/multilingual-e5-small-onnx-qint8 (HF)](https://huggingface.co/deepfile/multilingual-e5-small-onnx-qint8)
- [FastEmbed (qdrant)](https://github.com/qdrant/fastembed)
- [Sentence Transformers — Speeding up Inference](https://sbert.net/docs/sentence_transformer/usage/efficiency.html)
- [How to compute embeddings 3X faster with quantization (Nixiesearch)](https://medium.com/nixiesearch/how-to-compute-llm-embeddings-3x-faster-with-model-quantization-25523d9b4ce5)
- [text-embedding-3-small — OpenAI](https://openai.com/index/new-embedding-models-and-api-updates/)

**Русский (ruMTEB):**
- [ruMTEB benchmark and Russian embedding model design (ACL/arXiv)](https://arxiv.org/abs/2408.12503)
- [GigaEmbeddings — Efficient Russian Embedding Model (arXiv)](https://arxiv.org/html/2510.22369v1)
