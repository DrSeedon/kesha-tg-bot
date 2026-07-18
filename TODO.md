# TODO

- [ ] **Деплой v2.6.0 на Contabo** — File RAG + preventive compact + session-limit fix + file-search fix. `git pull && systemctl restart kesha-bot-vps` + `pip install watchfiles>=0.24`. НЕ задеплоено (юзер сказал потом)
- [ ] **Processing watchdog** — убивать зависший Claude CLI если нет активности N минут. Текущий stall detection (120s) ловит только паузы между stream-чанками, не зависание внутри tool-вызова
- [ ] **urgent_llm delivery guarantee** — доставка urgent_llm best-effort (fire-and-forget через ChatState). Если хендлер упал — напоминалка теряется. Codex отметил как архитектурное ограничение
- [ ] **Compact durable handoff** — при падении между reset и preamble контекст теряется
- [ ] **Мост Кеша↔Orchestra** (запрос Александра, НЕ срочно) — дать боту дёргать Orchestra-агентов
- [ ] **Ozon фильтры** — бренд/тип телескопа/etc в ozon_search. Фасеты есть в raw JSON (research #9 подтвердил), category-dynamic. Medium effort
- [ ] **Inject batching** — при множественных inject'ах за <500ms склеивать в один query
- [ ] **RAG diary-templates шум** — 1061 near-empty дневник может засорять retrieval (skip-empty guard ловит большинство). Мониторить, при нужде — min-real-chars порог
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling
- [ ] Rate limiting per-user
