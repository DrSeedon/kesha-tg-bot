# TODO

- [ ] **Processing watchdog** — убивать зависший Claude CLI если нет активности N минут. Текущий stall detection (120s) ловит только паузы между stream-чанками, не зависание внутри tool-вызова
- [ ] **urgent_llm delivery guarantee** — доставка urgent_llm best-effort (fire-and-forget через ChatState). Если хендлер упал — напоминалка теряется. Codex отметил как архитектурное ограничение
- [ ] **Compact durable handoff** — при падении между reset и preamble контекст теряется
- [ ] **Мост Кеша↔Orchestra** (запрос Александра, НЕ срочно) — дать боту дёргать Orchestra-агентов
- [ ] **Ozon фильтры** — бренд/тип телескопа/etc в ozon_search. Фасеты есть в raw JSON (research #9 подтвердил), category-dynamic. Medium effort
- [ ] **Inject batching** — при множественных inject'ах за <500ms склеивать в один query
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling
- [ ] Rate limiting per-user
