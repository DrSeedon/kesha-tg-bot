# TODO

- [ ] **Processing watchdog** — убивать зависший Claude CLI если нет активности N минут. Текущий stall detection (120s) ловит только паузы между stream-чанками, не зависание внутри tool-вызова
- [ ] **urgent_llm delivery guarantee** — доставка urgent_llm best-effort (fire-and-forget через ChatState). Если хендлер упал — напоминалка теряется. Codex отметил как архитектурное ограничение. Фикс требует await завершения ChatState processing, что ломает non-blocking flow
- [ ] **Compact durable handoff** — при падении между reset и preamble контекст теряется (сейчас: ok=False + юзер уведомлён, может повторить /compact). Идеал: писать summary в файл до reset
- [ ] **Мост Кеша↔Orchestra** (запрос Александра, НЕ срочно, ждёт Максима) — дать боту дёргать Orchestra-агентов: спавн воркеров, отправка сообщений, статусы. Через REST API `http://147.45.101.84:8888` (Bearer INTERNAL_TOKEN) + SSH-туннель/whitelist по IP. Реализация: MCP-тулы orchestra_spawn/send/status в kesha_tools.py
- [ ] **RAG auto-inject** (опц.) — авто-подмешивать top-K из истории в контекст перед ответом. Сейчас tool-based (search_memory, e5-small int8, 4.3/5 качество). Решить после наблюдения
- [ ] **RAG: лучшая модель при апгрейде VPS** — e5-large (5/5) OOM'ит 2.9GB VPS. При апгрейде RAM до 4GB+ или переносе на NL VPS (11GB) — переключить MODEL_NAME на e5-large
- [ ] **Inject batching** — при множественных inject'ах за <500ms склеивать в один query (сейчас 20 forwarded = 20 отдельных inject → Claude получает 20 прерываний)
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling
- [ ] Rate limiting per-user
