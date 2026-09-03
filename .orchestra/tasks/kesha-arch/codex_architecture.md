---
slug: architecture
topic: Kesha TG Bot architecture review
created: 2026-06-02T07:50:07+02:00
model: gpt-5.5
---

## Tests

Прочитаны `docs/tasks/kesha-arch/architecture-map.md`, все 14 корневых `.py` файла, `CHANGELOG.md`, `TODO.md`.

`pytest`:

```text
platform linux -- Python 3.13.7, pytest-9.0.2
rootdir: /mnt/data/Projects/Python/orchestra
collected 0 items
exit code 5
warning: pytest cache could not write to /mnt/data/Projects/Python/orchestra/.pytest_cache (read-only)
```

Тестов в проекте не найдено. Это не падение тестов, а отсутствие test suite; отдельно важно, что локальный pytest подтянул `pyproject.toml` из родительского проекта и запустился на Python 3.13.7, хотя проект заявлен как Python 3.12.

## Round 1 — 2026-06-02T07:50:07+02:00

### Summary

Архитектура рабочая для текущего масштаба: 2 активных пользователя, один VPS, solo dev, MVP уже в production. Главная проблема не в flat file structure и не в том, что `ChatState` большой, а в нескольких lifecycle/async edge cases вокруг отмены, inject, reminder delivery и chat routing. Flat layout при 14 файлах выглядит нормально: это не библиотека, не пакет с публичным API и не multi-service проект. `ChatState` действительно тянет много обязанностей, но они в основном сцеплены одной FSM; преждевременный распил даст больше callback wiring, если не начать с тестов. Coupling через setters и `_bot_ref` некрасивый, но терпимый; опасен не сам late binding, а fail-open fallback в chat resolution. Retry loop в `response_stream.py` перегружен и уже стал местом, где легко нарушить cancellation semantics. SQLite reminders и отдельные модули `media.py`, `telegram_io.py`, `compact.py`, `tool_status.py` в целом соответствуют философии проекта. Вердикт: не выкидывать, не тащить enterprise patterns, но закрыть несколько точечных рисков перед дальнейшим production-ростом.

### Замечания

blocking: `response_stream.py:286` — внешний retry loop ловит `asyncio.CancelledError` вместе с обычными exception и превращает отмену task в retry/reconnect/сообщение пользователю; при shutdown или cancellation это может удержать `_processing_task` живым и сломать graceful stop → вынести `except asyncio.CancelledError: raise` перед общим `except`, а cleanup делать в `finally` без подавления отмены.

blocking: `chat_state.py:620` — `ChatRegistry.shutdown()` только `cancel()`-ит debounce/processing tasks, но не ставит `_shutdown=True`, не await-ит tasks и не закрывает Claude clients; в сочетании с swallowed cancellation это оставляет фоновые корутины после shutdown → под lock перевести каждый `ChatState` в shutdown, отменить tasks, `await asyncio.gather(..., return_exceptions=True)`, затем disconnect/reset client при необходимости.

blocking: `kesha_tools.py:34` — `_resolve_chat()` fail-open-ит в `next(iter(ALLOWED))`, поэтому при потере `ContextVar` chat-bound tools могут отправить файл/реакцию/статус не тому пользователю; история v2.0.2 показывает, что это не теоретический класс бага → для `send_*`, `react`, reminders и status fail closed: если current chat не установлен, вернуть MCP error, а fallback оставить только для явно нечувствительных операций.

blocking: `claude_session.py:215` — `inject()` не сериализован: два concurrent Telegram handler task во время `PROCESSING` могут одновременно пройти `_is_processing` check и вызвать `_client.query()` на одном `ClaudeSDKClient`; прошлые баги уже признавали concurrent `query()` undefined behavior → добавить session-level `asyncio.Lock` вокруг `query()`/`_expected_results += 1` в `inject()` или очередь inject-запросов внутри `ChatState`, чтобы в SDK входил один writer за раз.

suggestion: `reminders.py:357` — `get_lazy_block_for_prompt()` помечает lazy reminders delivered до того, как `_ask_fn` реально успешно принял prompt; если `chat_state.py:446` упадёт, lazy reminder потерян как доставленный → возвращать `(block, ids)` и marked delivered делать после успешного `_ask_fn`, либо ввести состояние `claimed`/`delivered`.

suggestion: `reminders.py:298` — urgent reminders запускаются через `create_task`, а `reminders.py:323` помечает `delivered=1` после `run_urgent_prompt()`, который для idle case в `chat_state.py:283` только создаёт processing task и сразу возвращает; это best-effort enqueue, не delivery guarantee → либо переименовать семантику в `queued`, либо дать `run_urgent_prompt()` awaitable completion/future для reminder delivery.

suggestion: `bot.py:133` — `ChatRegistry` wiring принимает 11 параметров, из них несколько callbacks; пока это терпимо, но добавление ещё одного сервиса будет размазывать bootstrap дальше → заменить на маленькую dataclass `ChatRuntimeDeps`/`Services` без полноценного DI-контейнера.

suggestion: `chat_state.py:52` — `ChatState` получает `set_current_chat_fn` и `work_dir`, но сам класс их не использует; это усиливает впечатление god object и усложняет тестирование → убрать неиспользуемые deps из `ChatState`, оставить их в `ChatRegistry`.

suggestion: `chat_state.py:381` — `_run_batch()` занимается prompt assembly, lazy reminder injection, preview logging, вызовом Claude, auto-compact и error response; это ещё не катастрофа, но это главный кандидат на аккуратное извлечение → первым безопасным шагом вынести pure helper `build_batch_prompt(batch, lazy_block)` и покрыть его тестами.

suggestion: `handlers.py:270` — при ошибке Deepgram `h_voice()` вызывает `transcription_finished(None, ...)` и отправляет пользователю ошибку, но не передаёт voice file в LLM; `h_video_note()` в аналогичной ситуации делает fallback entry с `[video_note: path]` → для voice тоже enqueue fallback `[voice: path]` плюс error note, чтобы сообщение не исчезало из контекста.

suggestion: `inbox_server.py:29` — `/inbox` принимает `chat_id` из JSON без проверки против `ALLOWED`; bind на `127.0.0.1` снижает риск, но локальный отправитель всё равно может заставить бота писать в произвольный chat_id → если `ALLOWED` непустой, reject неизвестный `chat_id`.

thought: `handlers.py:39` и `kesha_tools.py:15` — module-level globals через setters выглядят грубо, но для aiogram bootstrap это нормальный pragmatic late binding; переписывать на классы сейчас стоит только если появятся тесты handlers или второй bot instance в одном процессе.

thought: `bot.py:16` — flat imports из корня (`from config import ...`) работают, пока запуск всегда `python bot.py`/systemd из project root; при 14 файлах это OK → переезд в package имеет смысл только вместе с test suite, `python -m kesha_bot`, явным layout и импортными smoke tests.

thought: `chat_state.py:381` — `_run_batch` вне lock не является багом само по себе; это правильнее, чем держать lock во время сетевого streaming turn. Реальные race conditions находятся на границе `phase == PROCESSING` → `session.inject()`, а не в самом факте, что processing идёт вне lock.

thought: `telegram_io.py:30` и `telegram_io.py:52` — `extract_text_with_urls()` / `extract_caption_with_urls()` почти одинаковые; это мелкая duplication, но легко чинится helper-ом `_extract_with_urls(text, entities)` без архитектурного распила.

thought: `kesha_tools.py:112`, `kesha_tools.py:132`, `kesha_tools.py:261`, `kesha_tools.py:281`, `kesha_tools.py:301` — 5 `send_*` MCP tools дублируют pattern resolve/check/path/FSInputFile/send/catch; можно убрать через маленький helper, но это не production-risk, пока tools стабильны.

thought: `setup_wizard.py:24` — wizard не импортируется и живёт как standalone entrypoint; оставлять можно, но README/systemd docs должны явно говорить, используется ли он ещё. Удалять только если onboarding больше не нужен.

question: `compact.py:94` — compaction сбрасывает session до durable handoff summary; TODO уже фиксирует риск падения между reset и preamble. Для 2 пользователей это приемлемый known tradeoff, но если compaction станет частой, summary стоит писать во временный файл до `reset_async()`.

### Что оставить как есть

- Flat root layout оставить как есть на текущем масштабе. Package ради package не даст пользы; сначала нужны тесты и стабильный entrypoint.
- Per-chat `ChatState` + `asyncio.Lock` + явные фазы оставить. Это хороший компромисс после удаления глобальных dict/set.
- `_run_batch()` не держит lock во время Claude streaming — это правильно.
- `compact.py` как отдельный модуль оставить: error handling там заметно чище, чем в streaming/retry path.
- SQLite reminders с WAL оставить. Для одного процесса и двух пользователей это проще и надёжнее Redis/failover.
- `telegram_io.py`, `media.py`, `tool_status.py` оставить отдельными utility modules; они достаточно cohesive.
- Late binding через `set_bot()` можно оставить, пока проект не пишет unit tests для handlers/tools. Главный фикс — fail closed в chat routing, не тотальный DI.
- Удаление failover/Redis было правильным. Возвращать distributed complexity для одного VPS не надо.

### Вердикт

требует доработки

Не из-за flat structure и не из-за размера `ChatState`, а из-за 3-4 конкретных async/lifecycle рисков: cancellation swallowing, shutdown без await, fail-open chat routing и concurrent inject в один SDK client. После этих фиксов архитектура достаточно здравая для MVP → small production на текущем масштабе.

## Round 2 — 2026-06-02T07:54:36+02:00

### Ответы на контраргументы Claude

AGREE: `response_stream.py:286` остается blocking. `except (Exception, asyncio.CancelledError)` действительно ломает cancellation semantics: shutdown/stop превращается в retry path. Нужен отдельный `except asyncio.CancelledError: raise` перед общим `except`. Дополнительно стоит пересмотреть внутренний `except asyncio.CancelledError` на `response_stream.py:278`: если это cleanup внутри retry, он не должен молча продолжать внешний цикл после отмены task.

PARTIAL: `chat_state.py:620` можно понизить с blocking до suggestion/high. Аргумент про `asyncio.run()` валиден для обычного process shutdown: после выхода из `main()` loop отменит оставшиеся tasks. Но текущий `shutdown()` все равно не является корректным lifecycle API: он не ставит `_shutdown=True`, не await-ит cancelled tasks, не дает им выполнить cleanup и не закрывает Claude clients. Это не главный production blocker после фикса `CancelledError`, но это первый hygiene fix для тестов, рестартов и будущего hot reload.

AGREE: `kesha_tools.py:34` остается blocking. Fail-open через `next(iter(ALLOWED))` для chat-bound tools недопустим, особенно с историей v2.0.2 про отправку файла не тому пользователю. Для `send_*`, `react`, reminders/status и любых действий с Telegram side effects нужен fail-closed при отсутствии current chat.

DISAGREE: `claude_session.py:215` я оставляю blocking. `ChatState._lock` сериализует только принятие решения, но `inject()` вызывается после выхода из lock (`chat_state.py:125`, `chat_state.py:183`). Окно гонки — это не микромомент, а весь `await self._client.query(text)` (`claude_session.py:219`). Если пользователь быстро отправит два сообщения во время `PROCESSING`, второй handler может войти, выйти из lock и вызвать `query()` на том же `ClaudeSDKClient`, пока первый `query()` еще не завершился. При признанном undefined behavior concurrent `query()` это достаточно серьезно для blocking, тем более фикс дешевый: session-level `asyncio.Lock` вокруг `query()` и `_expected_results += 1`.

### Обновлённые severity

blocking: `response_stream.py:286` cancellation swallowed. Без изменения.

suggestion/high: `chat_state.py:620` shutdown без await. Понижено с blocking, потому что обычный `asyncio.run()` process shutdown действительно добьет оставшиеся tasks, если cancellation больше не swallowed. Оставить в high-priority cleanup.

blocking: `kesha_tools.py:34` fail-open chat resolution. Без изменения.

blocking: `claude_session.py:215` concurrent inject. Без изменения: низкая частота не компенсирует высокий impact и простоту фикса.

### Ответы на вопросы

1. Да, согласен понизить shutdown без await до suggestion/high после фикса swallowed cancellation. Но я бы все равно сделал `await gather(..., return_exceptions=True)` в ближайшем MVP patch, потому что это маленький код и нормализует lifecycle.

2. Не согласен, что concurrent inject — только micro-timing race. Lock в `ChatState` отпускается до сетевого `query()`, поэтому реальное окно равно длительности `ClaudeSDKClient.query()`. Для одного пользователя "два быстрых сообщения во время ответа" — нормальный сценарий Telegram, не экзотика. Оставляю blocking.

3. Highest-priority suggestions для MVP: shutdown cleanup; reminder delivery semantics (`lazy` не mark delivered до успешного enqueue, urgent не называть delivered если это только queued); `/inbox` проверять `chat_id` против `ALLOWED`; voice fallback при ошибке Deepgram; маленький helper для `send_*` tools только после fail-closed routing.

### Prioritized action plan

1. Исправить cancellation в `response_stream.py`: отдельный `except asyncio.CancelledError: raise`, проверить внутренний cleanup catch, чтобы отмена не превращалась в retry.

2. Исправить `_resolve_chat()`/chat-bound tools в `kesha_tools.py`: fail closed без current chat, fallback оставить только для явно global/config operations, если такие есть.

3. Добавить `asyncio.Lock` в `ClaudeSession.inject()`: lock должен покрывать `_client.query(text)` и `_expected_results += 1`; не держать его вокруг streaming `send_message()`.

4. Почистить `ChatRegistry.shutdown()`: поставить `_shutdown=True` на chat states, отменить debounce/processing tasks, `await asyncio.gather(..., return_exceptions=True)`, затем clear registry. Если есть доступный disconnect/reset Claude client — вызвать его явно.

5. Закрыть top MVP suggestions: reminder delivered/queued semantics, `/inbox` allowed check, voice fallback.

6. Добавить минимальные regression tests или хотя бы async smoke tests на cancellation, fail-closed chat routing и serialized inject. Без test suite эти баги легко вернутся.

### Вердикт раунда

требует ещё фиксов

После уточнений один blocking понижен, но два ACK-блокера остаются, и concurrent inject я всё еще считаю blocker. Архитектура в целом приемлема для MVP, но перед спокойным production-ростом нужно закрыть cancellation, fail-closed routing и serialized inject.

## Round 3 — 2026-06-02T07:56:01+02:00

### Consensus

CONSENSUS REACHED.

Blocking к исправлению перед спокойным production-ростом:

1. `response_stream.py:286` — `asyncio.CancelledError` не должен попадать в retry path. Нужен отдельный `except asyncio.CancelledError: raise` перед общим `except`.
2. `kesha_tools.py:34` — `_resolve_chat()` должен fail closed для chat-bound tools. Fallback на `next(iter(ALLOWED))` нельзя использовать для `send_*`, `react`, reminders/status и любых Telegram side effects.
3. `claude_session.py:215` — `inject()` должен быть сериализован. Окно гонки равно длительности `await _client.query()`, поэтому нужен session-level `asyncio.Lock` вокруг `query()` и `_expected_results += 1`.

Suggestion/high к исправлению следующим patch:

1. `chat_state.py:620` — `shutdown()` должен ставить `_shutdown=True`, отменять tasks и `await asyncio.gather(..., return_exceptions=True)`.
2. `reminders.py:357` — lazy reminders нельзя помечать delivered до успешного enqueue через `_ask_fn`.
3. `inbox_server.py:29` — `/inbox` должен проверять `chat_id` против `ALLOWED`, если allowlist задан.
4. `handlers.py:270` — при ошибке Deepgram voice message должен попадать в LLM context как fallback `[voice: path]` плюс error note.

Оставить как есть:

- Flat file structure при 14 файлах нормальна; package layout имеет смысл только вместе с test suite и стабильным `python -m ...` entrypoint.
- `ChatState` как крупная FSM приемлем: responsibilities сцеплены, преждевременный распил даст больше wiring.
- Late binding через `set_bot()` прагматичен для aiogram bootstrap; чинить только при появлении tests/second bot instance.
- `_run_batch()` вне lock — правильно, lock не нужен во время Claude streaming.
- `compact.py`, `media.py`, `telegram_io.py`, `tool_status.py` остаются utility modules.
- SQLite reminders с WAL правильны для single-node VPS.
- Дублирование 5 `send_*` MCP tools и `extract_text/caption_with_urls` — cosmetic, не production risk.

### Final action plan (prioritized)

1. `response_stream.py`: добавить `except asyncio.CancelledError: raise` перед `except Exception`; проверить внутренний cleanup catch на `response_stream.py:278`, чтобы task cancellation не превращалась в retry/reconnect.

2. `kesha_tools.py`: заменить `_resolve_chat()`/использование fallback для chat-bound tools на fail-closed path. Для `send_*`, `react`, reminders/status возвращать MCP error, если `ContextVar` current chat пустой.

3. `claude_session.py`: добавить `asyncio.Lock` для `inject()`; внутри lock повторно проверить `_client`, `_connected`, `_is_processing`, затем выполнить `await _client.query(text)` и только после успеха увеличить `_expected_results`.

4. `chat_state.py`: улучшить `ChatRegistry.shutdown()` — пометить все `ChatState` как shutdown, отменить debounce/processing tasks, `await gather(..., return_exceptions=True)`, затем clear registry и при возможности закрыть Claude clients.

5. `reminders.py` + `chat_state.py`: поменять lazy reminder flow так, чтобы `delivered=1` ставился после успешного `_ask_fn`/enqueue, а не до него. Для urgent reminders уточнить терминологию `queued` vs `delivered`, если completion не await-ится.

6. `inbox_server.py`: reject неизвестный `chat_id`, если `ALLOWED` непустой.

7. `handlers.py`: для voice при ошибке Deepgram добавить fallback entry `[voice: path]` в контекст, аналогично video note.

8. Добавить минимальные async regression/smoke tests на cancellation propagation, fail-closed chat routing и serialized inject. Это важнее косметического package split.

### Вердикт

APPROVED (consensus reached)
