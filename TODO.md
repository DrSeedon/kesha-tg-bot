# TODO

- [ ] **Processing watchdog** — убивать зависший Claude CLI если нет активности N минут. Текущий stall detection (120s) ловит только паузы между stream-чанками, не зависание внутри tool-вызова
- [ ] **urgent_llm delivery guarantee** — доставка urgent_llm best-effort (fire-and-forget через ChatState). Если хендлер упал — напоминалка теряется. Codex отметил как архитектурное ограничение
- [ ] **Compact durable handoff** — при падении между reset и preamble контекст теряется
- [ ] **Молчаливый нативный компакт (#35, предсуществующий)** — если `_do_native_compact` падает на `get_context_usage()` ДО первого notify, внешний `except Exception` в `_do_compact` пишет лог и юзеру не говорит ничего. Нужен новый ключ STRINGS. Стало достижимее после #35: неизмеримый контекст больше не отбивает `/compact`, а пропускает его дальше
- [ ] **Устаревшее правило в памяти Кеши** — в саммари живёт заметка от 01.09 «детектор режет ответы с английским `session limit`, писать о лимитах по-русски». #33 это починил (`claude_session.py:490` требует `msg.is_error` либо отсутствие видимого вывода), защита в проде — заметка заставляет его коверкать формулировки на ровном месте
- [ ] **Crash-durable admission compact (#34 gap)** — до первого `log_user` исходный batch живёт только в RAM; падение бота во время preflight-компакта теряет уже принятый Telegram update. Для гарантии после рестарта нужен durable inbox/outbox с телом batch
- [ ] **Safe context-limit replay (#34 gap)** — повторять исходный batch после provider `context_limit` можно только при доказанных zero assistant/tool side effects и отсутствии либо rollback сохранённого input; текущий chunk-контракт этого не доказывает
- [ ] **Codex exact 95% upstream gap (#34)** — native auto-compact Codex ограничен максимумом 90% и не отключается текущей схемой; Kesha может гарантировать только compact не позже своего 95%-потолка. Пересмотреть при изменении app-server
- [ ] **Claude compact rollback не byte-identical (#34)** — summary-turn может изменить старый transcript/выполнить файловые действия; rollback сохраняет SID, но не отменяет эти эффекты
- [ ] **Мост Кеша↔Orchestra** (запрос Александра, НЕ срочно) — дать боту дёргать Orchestra-агентов
- [ ] **Ozon фильтры** — бренд/тип телескопа/etc в ozon_search. Фасеты есть в raw JSON (research #9 подтвердил), category-dynamic. Medium effort
- [ ] **Inject batching** — при множественных inject'ах за <500ms склеивать в один query
- [ ] **RAG diary-templates шум** — 1061 near-empty дневник может засорять retrieval (skip-empty guard ловит большинство). Мониторить, при нужде — min-real-chars порог
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling
- [ ] Rate limiting per-user
