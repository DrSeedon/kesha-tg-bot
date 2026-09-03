# Codex review — план #8 (bge-m3 int8 миграция)

**Модель:** GPT-5.5 (Codex exec). **Вердикт: APPROVE.**

## Summary

План корректно покрывает критичные отличия bge-m3 от текущего e5-small: custom `MODEL_NAME`, single-file `model_quantized.onnx`, `DIM=1024`, `SCHEMA_VERSION=7`, `PoolingType.CLS`, отсутствие `query:`/`passage:` префиксов и rebuild производного `vec.db` из read-only `messages.db`.

По текущему `rag.py` миграция 6→7 должна безопасно дропнуть `vec_messages`, `fts_messages`, `indexed` и пересоздать индекс с новой размерностью. Hybrid search/RRF/FTS prefix-expansion модельно-независимы, план правильно оставляет их без изменений.

## Findings

- **blocking: none.**
- **suggestion:** В AC T1 `python -c "import rag"` проверяет только импорт, не ловит главный риск — фактическую загрузку custom FastEmbed/ONNX и `add_custom_model` с `PoolingType.CLS`. T2 (реальный `test_index_and_search_semantic`) это покрывает → не блокер. Но для smoke на VPS полезнее короткий embed/search smoke. → **ПРИНЯТО: embed-smoke добавлен в AC T3 (деплой).**
- **suggestion:** Из-за single-thread RAG executor во время backfill ждут не только search, но и новые index operations. Данных не теряет: сообщения уже в `messages.db`, `indexed` делает backfill/index idempotent. → **ПРИНЯТО: добавлено в раздел Backfill/деплой.**

## Verdict

**APPROVE.** План можно отдавать в реализацию. Blocking-рисков по старту бота, hybrid search, backfill, миграции `DIM 384→1024` или сохранности `messages.db` не вижу.
