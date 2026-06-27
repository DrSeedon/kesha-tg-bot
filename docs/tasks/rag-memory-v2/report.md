# RAG v2 — final report

**Задача:** качество RAG 3/5 → улучшить. Абстрактные русские запросы проваливались, "ссора с Катей" не находился.

## Результат — провальные кейсы юзера ИСПРАВЛЕНЫ (измерено end-to-end на реальной e5-large)

| Кейс | Было | Стало |
|------|------|-------|
| "ссора с девушкой" → "Катя заебала, что расстаться" | не находил | **RANK-1** ✅ |
| "настройки AI" → "подкрутил промпт боту" | 1.5/5 | **RANK-1** ✅ |

Relevance замеры (E5 vs старый MiniLM): "ссора" 0.80 vs 0.43; "AI" 0.81 vs 0.28.

## Что сделано (3 части)

### A. Модель MiniLM → multilingual-e5-large int8
- `keisuke-miyako/multilingual-e5-large-onnx-int8` (561MB, dim 384→1024), `add_custom_model` (нет нативно в FastEmbed).
- Топ non-instruct ruMTEB. Latency 15мс (vs 3мс MiniLM). Влезает: 561MB + 220MB бот = ~800MB из 3.5GB.
- Анизотропия E5 (все cosine 0.75-0.85) — не вводим порог, только top-K + RRF-ранги.

### B. Chunking длинных сообщений
- `_chunk`: content >1200 символов → куски ~800 с overlap 200. Голосовые на 500 слов больше не 1 размытый вектор.
- `chunk_id = parent*1000+idx`, колонка `parent_message_id`, дедуп по parent в `_vec_search`/`_fts_search`/`search`.
- Защита (Codex): cap `[:999]` (нет PK-collision) + `_split_oversized` (токены >800 символов режутся).

### C. FTS5 prefix-expansion (русская морфология)
- Диагностика: FTS5 индексирует русский, но без стемминга — "Катей"≠"Катя" → 0 хитов на склонениях.
- `_expand_query`: `"ссора Катей"` → `'"ссора"* OR "Катей"*'`. Ловит суффиксальные словоформы.

## Файлы

| Файл | Δ | Что |
|------|---|-----|
| `rag.py` | +137/-35 | модель, DIM 1024, `_chunk`/`_split_oversized`/`_dedup`/`_expand_query`, chunk-схема, SCHEMA_VERSION 2 |
| `tests/test_rag.py` | +57 | chunk/dedup/prefix/cap/oversized + long-message-indexing |
| `requirements.txt` | +1/-1 | `fastembed>=0.8.0` |
| `CHANGELOG.md` | +25 | v2.3.1 |

## Тесты

- **Прогнаны (model-free), 8 PASS:** rrf, chunk_short_vs_long, dedup, expand_query, long_message_chunks, chunk_caps_at_stride, chunk_splits_oversized, schema_migration.
- **Real-model integration** (e5-large через прокси): оба провальных кейса юзера → RANK-1. Backfill + search + chunking + prefix end-to-end ✅.
- **На VPS после деплоя:** прогнать model-dependent тесты (модель качается там).

## Codex review (2 раунда, `codex-review-impl.md`)

- **Round 1:** 2 blocking — fastembed floor (`>=0.4`→`>=0.8.0`), chunk_id PK-collision при 1000+ чанках (backfill loop). + 3 suggestion.
- **Фиксы:** floor bump; `_chunk` cap `[:999]` (idx≤998 → chunk_id < (parent+1)*1000, collision невозможна — Codex подтвердил математически) + `_split_oversized` + overlap-fix.
- **Round 2:** **APPROVED.**

## Breaking changes
Нет для API. `vec.db` ребилдится (SCHEMA_VERSION 1→2, dim 384≠1024) — индекс производный, backfill восстановит из messages.db при первом старте v2.

## Known issues / TODO
- ⚠️ **VPS latency e5-large** — проверить на VPS-CPU (нет AVX-VNNI → медленнее). Фолбэк e5-base int8 (280MB, dim768) если тормозит.
- ⚠️ **Ребилд при первом старте** — переэмбединг всей истории на E5 (на 3 дня данных — секунды; на 100K — минуты).
- 📌 **before/after date-фильтр** (latent, suggestion Codex #5) — фильтруется после candidate-gen, может вернуть <limit при забитом дате pool. НЕ исправлено: параметры не экспонированы в `search_memory` тул (только query/limit/role). Known-limitation, фикс если понадобятся date-запросы.
- 📌 **pool*3 headroom** — на 2 юзера хватает; при росте до многих юзеров с длинными сообщениями добирать parent циклом.
