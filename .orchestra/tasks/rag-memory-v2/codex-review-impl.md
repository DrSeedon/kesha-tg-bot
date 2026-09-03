---
slug: v2-impl
model: gpt-5.5
---

## Summary

Ревью `rag.py` целиком и diff v2. Основная логика parent-dedup/RRF выглядит согласованно: `_vec_search` и `_fts_search` возвращают `parent_message_id`, дедуп сохраняет лучший rank, финальный `search()` джойнится к `messages.db` по parent id. Миграция v1→v2 через `SCHEMA_VERSION=2` корректно дропает несовместимые vec/FTS/indexed таблицы и должна ребилдиться из `messages.db`.

Но есть два места, которые могут сломать прод: зависимость FastEmbed не закреплена под новый custom-model API, а `chunk_id = parent*1000+idx` не защищён от выхода `idx` за stride.

## Findings

- blocking: `requirements.txt:8`, `rag.py:119-127` — код начал использовать `TextEmbedding.add_custom_model`, `ModelSource`, `PoolingType` и `model_file`, но зависимость осталась `fastembed>=0.4`. На VPS с уже установленной старой версией, которая удовлетворяет `>=0.4`, первый `_embed()` упадёт до любого поиска/бекфилла. Фикс: поднять нижнюю границу до версии, на которой реально проверен этот API и репозиторий модели (`fastembed>=0.8.0` по research), либо добавить явную проверку версии с понятной ошибкой.

- blocking: `rag.py:35`, `rag.py:147-157`, `rag.py:270-282` — `chunk_id = message_id * CHUNK_STRIDE + idx` не проверяет `idx < CHUNK_STRIDE`. Если одно сообщение даст 1000+ чанков, его `idx=1000` пересечётся с `chunk_id` следующего parent (`(id+1)*1000+0`), insert в `vec_messages` упадёт по primary key, транзакция откатится, а `backfill()` будет снова брать тот же неиндексированный batch и падать при каждом старте. Фикс: либо guard с явной ошибкой до insert, либо stride с большим запасом плюс тест, либо отдельный stable chunk id без арифметического пересечения.

- suggestion: `rag.py:42-63` — `_chunk()` режет только по словам, поэтому один токен длиннее `CHUNK_SIZE` уходит в chunk целиком. Для pasted URL/base64/log blob это обходит лимит, может дать тяжёлую/усечённую токенизацию в ONNX и ломает предположение теста про размер чанков. Фикс: если `len(w) > CHUNK_SIZE`, резать такой токен char-window'ом или ограниченно пропускать/сжимать его.

- suggestion: `rag.py:163-172`, `rag.py:183-201` — `LIMIT pool*3` применяется до дедупа по parent. Длинное релевантное сообщение с большим числом чанков может занять весь candidate pool, после `_dedup()` останется 1 parent, и `search()` вернёт меньше `limit`, хотя ниже были другие сообщения. Фикс: добирать кандидатов до `pool` уникальных parent циклом с увеличенным LIMIT/offset или выбирать лучший chunk per parent в SQL/постобработке с большим cap.

- suggestion: `rag.py:224-245` — `before/after` фильтруются только после vector/FTS candidate generation. Если top pool забит сообщениями вне даты, финальный join их выкинет и вернёт пусто/меньше `limit`, хотя внутри окна есть совпадения. Chunking усиливает это, потому что один parent может занимать много pre-filter слотов. Фикс: хранить timestamp metadata в vec/FTS и фильтровать до KNN/FTS, либо при date-фильтрах расширять pool и добирать после post-filter.

## Verdict

Не готово к merge без фикса dependency floor и защиты `chunk_id` от переполнения stride. Остальное можно принять как follow-up, если текущая цель — быстро поднять качество RAG на реальных кейсах.

## Round 2 — re-review

Проверял только diff, тесты не запускал.

### Findings

- FIXED: `requirements.txt:8`, `rag.py:131-140` — нижняя граница поднята до `fastembed>=0.8.0`, что закрывает риск установки версии, где нет используемого custom-model API (`TextEmbedding.add_custom_model`, `ModelSource`, `PoolingType`, `model_file`). Блокер из Round 1 снят.

- FIXED: `rag.py:35`, `rag.py:53-77`, `rag.py:161-166`, `rag.py:281-286` — `_chunk()` теперь возвращает не больше `CHUNK_STRIDE - 1` чанков. При `CHUNK_STRIDE = 1000` это даёт `len(chunks) <= 999`, значит `idx ∈ [0, 998] = [0, STRIDE-2]`. Тогда `chunk_id = parent * STRIDE + idx <= parent * STRIDE + STRIDE - 2 = (parent + 1) * STRIDE - 2`, то есть строго меньше первого `chunk_id` следующего parent (`(parent + 1) * STRIDE`). PK collision на границе parent больше не достигается.

- suggestion: cap-фикс математически достаточен для защиты primary key, но он молча отбрасывает хвост экстремально длинного сообщения после 999 чанков. Для текущего масштаба это приемлемый trade-off, но если такие сообщения реальны, лучше логировать truncation с `message_id` и количеством отброшенных чанков, чтобы потеря recall была наблюдаемой.

- known-limitation: `before/after` по-прежнему фильтруются после candidate generation в `RagMemory.search()`, но `search_memory` сейчас не экспонирует эти параметры наружу. Это не блокирует текущий merge, пока публичный tool schema остаётся `query/limit/role`.

### Verdict

APPROVED. Оба blocking из Round 1 закрыты; новых blocking/regression по проверенному diff не вижу.
