# RAG апгрейд #8 — план внедрения (e5-small int8 → bge-m3 int8)

**Что:** заменить embedding-модель в `rag.py` на `bge-m3` (int8 ONNX, `AlpEge/bge-m3-onnx-int8`), DIM 384→1024, SCHEMA_VERSION 6→7, обобщить prefix/pooling-логику под не-E5 модель. Backfill 677 msgs на Contabo (по координации рестартов).

**Область:** только `rag.py` + `tests/test_rag.py`. `messages.db` НЕ трогается. `requirements.txt` без изменений (fastembed уже есть, модель — runtime download).

---

## Что меняется в rag.py (построчно)

| Константа/функция | Было | Станет |
|-------------------|------|--------|
| `MODEL_NAME` | `intfloat/multilingual-e5-small` | `AlpEge/bge-m3-onnx-int8` (кастомное имя ≠ нативному FastEmbed — иначе add_custom_model пропустится → fp32 → краш) |
| `MODEL_HF` | `Xenova/multilingual-e5-small` | `AlpEge/bge-m3-onnx-int8` |
| `MODEL_FILE` | `onnx/model_quantized.onnx` | `model_quantized.onnx` (single-file, self-contained int8 — НЕ fp32 с external onnx_data) |
| `DIM` | 384 | 1024 |
| `SCHEMA_VERSION` | 6 | 7 (дроп vec/fts/indexed → ребилд через backfill) |
| pooling в `add_custom_model` | `PoolingType.MEAN` | `PoolingType.CLS` (bge-m3 использует CLS-токен) |
| prefix-ветка `_embed` | `if "e5" in MODEL_NAME: query:/passage:` | обобщить: флаг `MODEL_PREFIX` (bool). bge → без префиксов |
| docstring модуля (строка 1) | «multilingual-e5-base» | «bge-m3 int8» (актуализировать) |
| коммент строка 19 | e5-small описание | bge-m3 описание |

### Ключевые правки

1. **Prefix-логика (строки 151-154).** Заменить `if "e5" in MODEL_NAME` на явный флаг:
   ```python
   MODEL_PREFIX = False  # bge-m3: без query:/passage:. E5-модели требуют True.
   ...
   if MODEL_PREFIX:
       prefix = "query: " if is_query else "passage: "
       texts = [prefix + t for t in texts]
   ```
   WHY: `"e5" in MODEL_NAME` для `AlpEge/bge-m3-onnx-int8` вернёт False (случайно правильно), но это хрупко и неявно. Явный флаг = «pit of success» — при следующей смене модели видно что крутить.

2. **pooling (строки 145-148).** `add_custom_model(..., pooling=PoolingType.CLS, ...)`.
   WHY: bge-m3 — CLS-пулинг. MEAN даст неправильные вектора (тихая деградация качества).

3. **`_expand_query` / FTS5 prefix-expansion — НЕ ТРОГАТЬ.** Это лексический слой (морфология русского в FTS5), не зависит от embedding-модели. Работает как есть. (Проверка: тест `test_expand_query_prefix` должен пройти без изменений.)

4. **Анизотропия / порог cosine — НЕ ВВОДИТЬ.** У bge margin шире (0.27-0.74), но абсолютный порог всё равно зарезал бы релевантное (rel-косинусы ~0.47). Оставляем top-K + RRF-ранги (как сейчас). Никаких изменений — просто фиксируем в плане что не делаем.

## Что меняется в tests/test_rag.py

- Тесты используют `rag.DIM` динамически (строка 174: `[[0.0]*rag.DIM ...]`) — DIM подхватится автоматически.
- `test_index_and_search_semantic` — реальный embed через bge-m3 (модель скачается в CI/локально). Смысловой ассерт («питон» в топе) должен пройти на bge (проверить локально — bge сильнее на семантике, риск низкий).
- Добавить: тест что `MODEL_PREFIX=False` → `_embed` НЕ добавляет "query:"/"passage:" (мокнуть embedder, проверить переданный текст).
- Backfill/idempotency/isolation-тесты — модель-агностичны, пройдут как есть.

## Backfill / деплой (Contabo)

1. `git pull` на `/opt/kesha-bot` (юзер kesha).
2. **Рестарт бота** → `_create_schema` видит user_version 6 ≠ 7 → дропает vec_messages/fts_messages/indexed → `PRAGMA user_version=7`.
3. Первый embed = холодная загрузка bge-m3 (~10-15с) + backfill 677 msgs (~6-8 мин на VPS AVX2). RAG недоступен во время backfill (не бот).
4. `messages.db` — только read-only ATTACH, НЕ модифицируется.
5. **Модель скачается на VPS при первом старте** (~570MB, Contabo Франция — прямой доступ, прокси не нужен). Проверить что скачивание не таймаутит.
6. **Single-thread RAG executor во время backfill** (Codex): backfill держит единственный rag_executor-поток → на это время ждут не только search, но и live index новых сообщений. Данные НЕ теряются: сообщения уже в `messages.db`, а `indexed` делает index/backfill идемпотентными — после backfill новые сообщёния доиндексируются штатно. Просто RAG ~6-8 мин «занят» (не бот).

## Rollback план

Если bge на Contabo хуже бенчмарка (качество/RAM/latency):
1. `git revert` коммита #8 (вернёт rag.py к e5-small, SCHEMA_VERSION 7→6).
2. Рестарт бота → `_create_schema` видит 7 ≠ 6 → дроп → backfill назад на e5-small (~1-2 мин, e5-small быстрее).
3. `messages.db` цел → откат чистый, потери данных нет.
   Итого откат ≈ 3-4 мин. WHY безопасно: индекс производный, дроп+ребилд идемпотентен, источник (messages.db) неизменен.

## Координация деплоя

⚠️ Коллега **feat-ozon-mcp** тоже деплоит на Contabo + рестартит `kesha-bot-vps`. Backfill требует рестарта бота. **НЕ рестартовать молча** — перед деплоем написать kesha-tg-bot, он разрулит очередь рестартов.

---

## Tickets

### T1 — Заменить модель на bge-m3 int8 в rag.py
- Files: `rag.py`
- Изменения: MODEL_NAME/MODEL_HF/MODEL_FILE → AlpEge/bge-m3-onnx-int8 + model_quantized.onnx; DIM 384→1024; SCHEMA_VERSION 6→7; pooling MEAN→CLS в add_custom_model; ввести `MODEL_PREFIX=False` и переписать prefix-ветку `_embed` на явный флаг; актуализировать docstring/комменты.
- AC:
  - `rag.DIM == 1024`, `rag.SCHEMA_VERSION == 7`, `rag.MODEL_NAME == "AlpEge/bge-m3-onnx-int8"`.
  - `add_custom_model` вызывается с `pooling=PoolingType.CLS` (MODEL_NAME не нативный → guard пропускает регистрацию).
  - `_embed` НЕ добавляет "query:"/"passage:" когда MODEL_PREFIX=False (проверить на моке embedder: переданный в embed текст == исходному).
  - `python -c "import rag"` без ошибок (smoke).
- blocked-by: none

### T2 — Обновить тесты под bge-m3 (DIM + prefix)
- Files: `tests/test_rag.py`
- Изменения: добавить тест что MODEL_PREFIX=False не добавляет префиксы; убедиться что DIM-зависимые места используют `rag.DIM`; прогнать смысловой тест на реальном bge.
- AC:
  - `UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest tests/test_rag.py -x -q` — все зелёные (реальный embed bge-m3 скачивается/кешируется).
  - `test_index_and_search_semantic` находит «питон»-сообщение на bge.
  - Новый тест prefix проходит.
- blocked-by: T1

### T3 — (деплой, ПОСЛЕ approve + координации) backfill на Contabo + замеры
- Files: — (операционный, не код)
- Изменения: git pull на VPS, рестарт (по координации с kesha-tg-bot), backfill, замер RAM/latency/качества на 6 контр-запросах.
- AC:
  - `messages.db` count до == после (677, не тронут).
  - vec.db user_version == 7, vec_messages непустой.
  - RAM kesha-процессов после загрузки bge < 3GB (бюджет ~2.4GB).
  - **embed/search smoke на VPS** (Codex): короткий live-прогон — `search()` возвращает результат, embedder реально загрузился с CLS-пулингом (не только `import rag`). Ловит краш загрузки модели/pooling.
  - Контр-запросы («ссора с Катей», «настройки промпт», «еда КБЖУ») возвращают релевантное в топе.
  - Бот отвечает, RAG search работает.
- blocked-by: T1, T2 (+ approve юзера + координация рестарта)

---

## Что НЕ трогаем
- `_expand_query` / FTS5 логику (лексический слой, модель-агностичен).
- Порог cosine (не вводим — top-K + RRF как есть).
- chunking-параметры (CHUNK_* — общие, отдельная тема).
- batch_size=16 + arena-off (OOM-фиксы — оставить, не мешают на 8GB).
- `messages.db`, reminders.db, prod до финального approve.
- Ozon MCP (#7, коллега).
