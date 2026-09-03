# RAG v2 — implementation plan

Все изменения в `rag.py` + тесты. `kesha_tools.py`/`bot.py`/`message_log.py` НЕ трогаем (API `search/index_message/backfill` сохраняется).

## 1. Модель → e5-large-int8

```python
MODEL_NAME = "keisuke-miyako/multilingual-e5-large-onnx-int8"
DIM = 1024
SCHEMA_VERSION = 2  # bump: dim 384→1024 + parent_message_id → дроп+ребилд
```
`_embed`: регистрация custom-модели при первом вызове:
```python
from fastembed import TextEmbedding
from fastembed.common.model_description import PoolingType, ModelSource
if MODEL_NAME not in {m["model"] for m in TextEmbedding.list_supported_models()}:
    TextEmbedding.add_custom_model(model=MODEL_NAME, pooling=PoolingType.MEAN, normalization=True,
        sources=ModelSource(hf=MODEL_NAME), dim=DIM, model_file="model_quantized.onnx")
self._embedder = TextEmbedding(model_name=MODEL_NAME)
```
E5-префиксы passage:/query: уже есть — сохранить.

## 2. Chunking длинных сообщений

- `CHUNK_CHAR_LIMIT = 1200` (~300 токенов рус.), `CHUNK_SIZE = 800` (~200 ток.), `CHUNK_OVERLAP = 200` (~50 ток.) — в символах (не тащить tiktoken).
- Новая `_chunk(content) -> list[str]`: если `len <= CHUNK_CHAR_LIMIT` → `[content]`. Иначе скользящее окно по словам с overlap.
- Схема vec0/fts: PK `chunk_id`, +`parent_message_id` метадата-колонка. `chunk_id = parent_message_id * 1000 + idx` (≤1000 чанков/сообщение).
- `indexed` keyed by `parent_message_id` (идемпотентность по исходному сообщению).
- `index_message`: чанкуем → embed список → вставляем N строк, все с одним parent_message_id.
- `search`: KNN/FTS возвращают `parent_message_id` (не chunk_id) → RRF по parent → дедуп (parent уже уникален после маппинга chunk→parent). Возврат как раньше.

Схема:
```sql
CREATE VIRTUAL TABLE vec_messages USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    parent_message_id INTEGER,
    chat_id INTEGER PARTITION KEY,
    role TEXT,
    embedding FLOAT[1024]
);
CREATE VIRTUAL TABLE fts_messages USING fts5(
    content, chat_id UNINDEXED, role UNINDEXED, parent_message_id UNINDEXED
);
CREATE TABLE indexed (message_id INTEGER PRIMARY KEY);  -- = parent_message_id
```
`_vec_search`/`_fts_search` возвращают `parent_message_id`, дедуп сохранением первого (лучшего) ранга.

## 3. FTS5 prefix-expansion (русская морфология)

`_fts_search`: строить MATCH-запрос как OR префиксов:
```python
def _expand(query):
    words = [w for w in re.findall(r'\w+', query) if len(w) >= 3]
    return " OR ".join(f'"{w}"*' for w in words) if words else None
```
`'ссора с Катей'` → `"ссора"* OR "Катей"*` — ловит "Катя"? Нет: prefix "Катей*" ⊅ "Катя". НО "ссора"* поймает "ссориться/ссорой". Частичная победа: prefix ловит суффиксальные словоформы (расст*→расстаться/расставание), не префиксальные склонения имён. Достаточно — основной сигнал теперь E5.
Фолбэк: если expand даёт пусто (все слова <3) → старый phrase-quote.

## 4. Дедуп в search

KNN/FTS теперь дают parent_message_id с повторами (несколько чанков одного сообщения). Сжать в уникальные сохранив порядок (лучший ранг) ДО RRF:
```python
def _dedup(ids): 
    seen=set(); return [x for x in ids if not (x in seen or seen.add(x))]
```

## Тесты (test_rag.py)
- обновить DIM→1024 в проверках
- test_chunk: длинный content → >1 chunk, overlap; короткий → 1
- test_chunk_search: длинное сообщение находится по фрагменту, дедуп по parent
- test_fts_prefix: словоформа находится через prefix
- SCHEMA_VERSION=2 миграция (старый dim384 дропается)
- model-dependent тесты — на VPS

## Риски
- chunk_id = parent*1000+idx: если parent_message_id огромный (>9e15/1000) — переполнение int64? messages.id автоинкремент, до 9e12 норм. ОК.
- E5 latency на VPS — проверить, фолбэк e5-base.
- Ребилд: SCHEMA_VERSION 2 дропнет dim384 индекс (несовместим) → backfill переэмбедит на E5.
