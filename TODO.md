# TODO

- [ ] **Processing watchdog** — убивать зависший Claude CLI если нет активности N минут. Текущий stall detection (120s) ловит только паузы между stream-чанками, не зависание внутри tool-вызова
- [ ] **urgent_llm delivery guarantee** — доставка urgent_llm best-effort (fire-and-forget через ChatState). Если хендлер упал — напоминалка теряется. Codex отметил как архитектурное ограничение. Фикс требует await завершения ChatState processing, что ломает non-blocking flow
- [ ] **Compact durable handoff** — при падении между reset и preamble контекст теряется (сейчас: ok=False + юзер уведомлён, может повторить /compact). Идеал: писать summary в файл до reset
- [ ] **Мост Кеша↔Orchestra** (запрос Александра, НЕ срочно, ждёт Максима) — дать боту дёргать Orchestra-агентов: спавн воркеров, отправка сообщений, статусы. Через REST API `http://147.45.101.84:8888` (Bearer INTERNAL_TOKEN) + SSH-туннель/whitelist по IP. Реализация: MCP-тулы orchestra_spawn/send/status в kesha_tools.py
- [ ] **RAG-память (векторный поиск по истории)** — zvec (Alibaba, embedded, гибридный поиск) или ChromaDB. Каждое сообщение → embedding → при запросе ищем релевантные из всей истории → inject в контекст. Решает проблему потери деталей при compact. MCP tool `search_memory`. messages.db уже есть, нужно: embedding модель (multilingual-e5-large локально или OpenAI), векторная БД, auto-inject перед ответом
- [ ] **Inject batching** — при множественных inject'ах за <500ms склеивать в один query (сейчас 20 forwarded = 20 отдельных inject → Claude получает 20 прерываний)
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling
- [ ] Rate limiting per-user
