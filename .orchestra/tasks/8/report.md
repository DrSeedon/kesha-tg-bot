# RAG апгрейд #8 — отчёт (T1+T2, локально)

**Статус:** T1+T2 закоммичены, тесты зелёные. T3 (деплой на Contabo) — НЕ выполнен, ждёт approve + координацию рестарта.

## Что сделано

**T1 — модель bge-m3 int8 в rag.py:**
- `MODEL_NAME/MODEL_HF` → `AlpEge/bge-m3-onnx-int8` (кастомное имя ≠ нативному FastEmbed → add_custom_model срабатывает)
- `MODEL_FILE` → `model_quantized.onnx` (single-file, self-contained int8)
- `DIM` 384 → 1024
- `SCHEMA_VERSION` 6 → 7 (дроп vec/fts/indexed → ребилд backfill)
- `add_custom_model(pooling=...)` → CLS через новый `MODEL_POOLING="cls"`
- Хрупкую ветку `if "e5" in MODEL_NAME` заменил на явный флаг `MODEL_PREFIX=False` (bge не получает query:/passage:)
- Актуализировал docstring + комменты

**T2 — тесты:**
- `test_no_e5_prefix_when_disabled` — MODEL_PREFIX=False → префиксы НЕ добавляются
- `test_e5_prefix_when_enabled` — MODEL_PREFIX=True → query:/passage: добавляются (регрессия E5-пути)
- DIM-зависимые тесты подхватывают `rag.DIM` динамически — прошли на 1024 без правок

## Файлы

| Файл | ±строк |
|------|--------|
| `rag.py` | +16/−11 |
| `tests/test_rag.py` | +34/−0 |

Хирургия соблюдена: `messages.db`, прочий код НЕ тронуты.

## Тесты

- `pytest tests/test_rag.py -x -q` → **17 passed in 9.1s** (включая реальный embed через bge-m3).
- **Live smoke (Codex-требование):** временные БД, реальная загрузка bge-m3 с CLS-пулингом:
  - schema user_version = 7 ✅
  - backfill 4 msgs за 2.7s (холодная загрузка модели включена)
  - vec dim 1024, 4 вектора
  - 3 абстрактных запроса («ссора с девушкой», «настройки промпт», «парсер маркетплейс») → релевантное сообщение на топ-1 ✅
  - (прод vec.db/messages.db НЕ тронуты — temp dir)

## Adversarial self-review

1. MODEL_NAME-guard: кастомное имя → add_custom_model с CLS. Проверено smoke (модель загрузилась, ранжирование корректно).
2. CLS реально применён: будь MEAN — вектора bge были бы мусором, ранжирование сломалось бы. Smoke ранжирует верно.
3. Prefix НЕ протекает: юнит-тест + smoke.
4. Миграция схемы: smoke показал user_version=7, backfill на dim 1024 без ошибок размерности.
5. Гипотетика: если репо AlpEge когда-то станет нативным в FastEmbed — guard пропустит регистрацию, возьмётся их дефолтный pooling. Не реально (community-репо), не блокер для 2 юзеров.

## Breaking

- SCHEMA_VERSION 6→7: при первом старте с новым кодом vec.db дропнется и переиндексируется (backfill). Это ожидаемо и есть суть задачи. `messages.db` цел.

## Commit

`0e96669  #8: RAG e5-small → bge-m3 int8 (separation margin 4x)`

## T3 — деплой на Contabo (ВЫПОЛНЕН, 2026-07-03)

- ✅ merge в main (ffa7ab8) → `git pull` на /opt/kesha-bot → рестарт kesha-bot-vps
- ✅ Schema migration v6→v7 (`dropped index, will rebuild via backfill`)
- ✅ bge-m3 загрузился: `RAG embedder loaded: AlpEge/bge-m3-onnx-int8` (570MB скачался напрямую, CLS-пулинг)
- ✅ Backfill: **685 msgs** за ~24 мин (VPS AVX2, ~0.47 msgs/s — много длинных голосовых → много чанков)

### Прод-замеры (боевые данные)

| Метрика | Бенчмарк (ноут) | Прод (Contabo) |
|---------|-----------------|----------------|
| Бот RSS | ~1826MB (peak с tracemalloc) | **1248MB** (steady-state) |
| Latency поиска | 22ms | **58-78ms** |
| Холодная загрузка | ~10-15s | 9s (однократно) |
| Качество (контрольные) | margin 4x | **5/5 запросов → топ-1** |

Контрольные запросы на проде (все дали релевантное на топ-1):
- «ссора с девушкой отношения расстаться» → «ссора с Катей...» ✅
- «настройки AI подкрутил промпт боту» → msg Максима про промпт/настройки ✅
- «что я ел сегодня еда КБЖУ» → «КБЖУ-чек! Что ел сегодня» ✅ (прежний провальный кейс!)
- «парсер Ozon скрейпинг товаров» → «Готовые парсеры Ozon на GitHub» ✅
- «психолог схема терапия состояние» → «Схема Самопожертвование» ✅ (прежний провальный кейс!)

### Health
- Бот active/running, single MainPID, **0 Conflict**, 0 errors/tracebacks за 30 мин
- **messages.db цел**: 685 до = 685 после (только эмбеддинги пересчитаны)
- RAM: available 6GB, swap 0 — OOM исключён, запас для Ozon Playwright есть

### Docs
- CHANGELOG.md → v2.5.0
- CLAUDE.md → RAG хронология + module table + search_memory описание → bge-m3

**Rollback НЕ понадобился** — деплой чистый.
