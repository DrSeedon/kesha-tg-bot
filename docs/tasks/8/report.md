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

## TODO (следующим ходом, по approve)

- **T3 деплой на Contabo:** git pull → рестарт (по координации с feat-ozon-mcp) → backfill 677 msgs (~6-8 мин) → замеры RAM/latency/качества на контр-запросах.
- Обновить CHANGELOG.md (новая версия) + CLAUDE.md (RAG session notes → bge-m3) — по факту деплоя.
