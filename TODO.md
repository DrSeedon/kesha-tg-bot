# TODO

- [ ] **Саммари компакта нигде не сохраняются (#22 блокер)** — 0 строк с `## OBJECTIVE` из 7265 в проде `messages.db`. Ни один компакт не оставляет следа, по которому можно сравнить старый и новый промпт. Плюс наших компактов всего 1 за 7 дней (остальные нативные). Пока не решено — любой замер качества сжатия невозможен
- [ ] **Processing watchdog** — убивать зависший Claude CLI если нет активности N минут. Текущий stall detection (120s) ловит только паузы между stream-чанками, не зависание внутри tool-вызова
- [ ] **urgent_llm delivery guarantee** — доставка urgent_llm best-effort (fire-and-forget через ChatState). Если хендлер упал — напоминалка теряется. Codex отметил как архитектурное ограничение
- [ ] **Compact durable handoff** — при падении между reset и preamble контекст теряется
- [ ] **Логи ОБОИХ рантаймов не попадают ни в journal, ни в файл (#V-37 + запрос seedon 19.09)** — `config.py:51` настраивает только логгер `kesha`, а `codex_session.py:39` и `claude_session.py:32` берут `logging.getLogger(__name__)`; у корневого логгера обработчика нет → INFO глушит `logging.lastResort` (WARNING+). Следствие помимо диагностики блоков: строка `Result: …, N turns, stop=…, cost=$X` (`claude_session.py:562`) не писалась НИКОГДА — 0 совпадений в journal за 28 суток и 0 в `logs/kesha.log*`. Стоимость ответа нигде не сохраняется: `/status` и `kesha_tools.py:103` показывают `total_cost_usd` живой сессии в памяти (обнуляется рестартом и `/clear`), в `messages.db` полей стоимости нет. Токены можно достать только из транскриптов Claude CLI (`~/.claude/projects/-opt-cog-second-brain/*.jsonl`, поля `message.usage`), долларов там нет
- [ ] **AGENTS.md из cwd грузится целиком в Codex-тред (#V-37)** — `/opt/cog-second-brain/AGENTS.md` весит 68 023 байта при лимите `project_doc_max_bytes = 131072`; уходит в каждый тред Кеши. Кандидат в причины блока и в любом случае лишний вес
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

- API-ошибка CLI уходит пользователю как ответ: 23.09 строка `API Error: 400 Claude Code 2.1.259 does not support this model` пришла Максиму обычным текстом (тот же класс, что капитуляция разбора тула в v2.9.3 — CLI отдаёт провал ассистентским текстом). Распознавать `API Error:` как ошибку рантайма и показывать русское сообщение.
