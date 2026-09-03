## Summary

Ну да, шестнадцать строк протокола победили архитектуру — жаль, что рядом прячется `session.py` на 1782 строки 😏

Направление рабочее, но главный вывод верен лишь наполовину:

- `compact` действительно не обязан входить в базовый backend-протокол.
- Но Orchestra всё равно обобщает управление компактом в общем session-слое: он выбирает runtime-specific реализацию и вызывает `compact_context()`.
- В Кеше такого нейтрального session-фасада пока нет: `ChatState`, `compact.py` и `response_stream.py` напрямую зависят от методов и состояния `ClaudeSession`.

В текущем плане два blocking-пробела: неполный runtime-контракт приведёт к падению Codex-пути, а HTTP-мост не имеет достаточной границы авторизации. Оценка 7–10 дней выглядит как best case, не как срок, на который стоит планировать.

Проверены только разрешённые файлы. Изменений и тестовых запусков не было.

## Findings (blocking/suggestion/question)

### [blocking] Runtime-протокол не покрывает обязательный путь обработки сообщения

В T1 заявлены только `session_id/send_message/inject/interrupt/reset_async/get_context_usage` ([research:230](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:230)). Но перед каждым запросом `ChatState` безусловно вызывает `check_context_reserve()` ([chat_state.py:631](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/chat_state.py:631)), retry-путь требует `reconnect()` ([response_stream.py:408](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/response_stream.py:408)), а shutdown вызывает приватный `_safe_disconnect()` ([chat_state.py:923](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/chat_state.py:923)). Реализация T4 по описанному контракту упадёт ещё до первого `send_message()`.

Нужен либо runtime-neutral session-фасад над адаптерами, либо контракт, реально соответствующий всем callers: reserve check, reconnect/disconnect, context usage и runtime telemetry. Узкий backend-протокол при этом можно сохранить.

### [blocking] `127.0.0.1` не авторизует привилегированный HTTP-мост

T3 ограничивает мост loopback-интерфейсом, но не задаёт аутентификацию, а `chat_id` предлагается сделать аргументом тула ([research:246](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:246)). Это позволяет любому локальному процессу либо самой модели выбрать чужой чат и вызвать отправку локального файла, реакцию, рестарт или SSH-команду. Текущая поверхность действительно привилегированная: `send_file` принимает произвольный путь ([kesha_tools.py:143](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/kesha_tools.py:143)), а `run_on_laptop` выполняет SSH-команды ([kesha_tools.py:478](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/kesha_tools.py:478)).

`chat_id` нельзя отдавать под контроль модели. MCP-процесс должен получать привязанный chat ID из доверенной конфигурации, а мост — проверять отдельный capability-token и сопоставлять `token → chat_id`. Unix socket может сузить поверхность, но не заменяет авторизацию.

### [suggestion] Вывод про compact сформулирован сильнее, чем доказывает Orchestra

Да, `BackendLike` состоит из шести операций и не содержит compact ([backend_protocol.py:8](/mnt/data/Projects/Python/orchestra/app/backend_protocol.py:8)). Но общий `AgentSession.compact()` делает runtime-dispatch, а затем вызывает необъявленный `compact_context()` через `getattr` ([session.py:1087](/mnt/data/Projects/Python/orchestra/app/session.py:1087), [session.py:1162](/mnt/data/Projects/Python/orchestra/app/session.py:1162)). Это и есть скрытый контракт.

Для Кеши Claude-транзакцию можно оставить без изменений, но вызывающий слой всё равно надо обобщить: текущий `compact_session()` требует `begin/start/commit/rollback_session_replacement()` ([compact.py:239](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/compact.py:239)), а `ChatState` хранит одну compact-функцию для любого runtime ([chat_state.py:759](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/chat_state.py:759)).

Итог: compact не нужен в базовом `Protocol`, но runtime-specific `compact()` нужен в фасаде, capability или реестре стратегий. Counter-evidence верный; дыра находится в основном вердикте и оценке.

### [suggestion] История session ID в Orchestra не реализует возврат в старую сессию

Документ утверждает, что запись старого ID в историю позволит затем зарезюмить Claude ([research:206](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:206)). Реальный `change_model()` только добавляет ID в `session_id_history`, сбрасывает текущий ID и нигде не извлекает старый при обратном переключении ([session.py:1533](/mnt/data/Projects/Python/orchestra/app/session.py:1533)). История там архивная, не resumable map.

У Кеши положение ещё жёстче: на чат хранится один файл с одним сырым Claude session ID ([claude_session.py:111](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/claude_session.py:111)). Для обещанного поведения нужен атомарно сохраняемый per-chat state:

- активный runtime;
- текущий ID каждого runtime;
- ожидающий handoff;
- позиция в message log, с которой конкретный runtime был приостановлен;
- rollback при неудачном connect/resume.

При возврате надо резюмить старую нативную сессию и передавать только Codex-era delta. Если native ID умер — создавать новую с полным handoff.

### [suggestion] `message_log` ещё не является полным источником handoff

При tool call `_finalize_text_block()` отправляет накопленный текст и очищает `parts` ([response_stream.py:216](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/response_stream.py:216)). В БД в конце записывается только содержимое оставшегося `parts` ([response_stream.py:578](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/response_stream.py:578)). Значит, ранние части ответа перед инструментами уже показаны пользователю, но в handoff не попадут.

Перед признанием handoff «почти бесплатным» нужно либо накапливать полный assistant transcript отдельно от Telegram-блоков, либо логировать каждый завершённый блок с общим turn ID. Tool results также сейчас не входят в provider-neutral историю.

### [suggestion] Пять «самодостаточных» MCP-тулов классифицированы неверно

В таблице `get_bot_status`, `toggle_debug` и `set_debounce` объявлены почти автономными ([research:155](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:155)), но:

- `set_debounce` мутирует живой `ChatState` и registry ([kesha_tools.py:47](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/kesha_tools.py:47));
- `toggle_debug` должен менять логирование процесса бота, а не отдельного MCP-процесса ([kesha_tools.py:64](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/kesha_tools.py:64));
- `get_bot_status` читает живую session, registry и media-состояние ([kesha_tools.py:74](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/kesha_tools.py:74)).

То есть мост нужен примерно для 14 из 16 тулов, а не только для медиа и chat-scoped операций. T3 в 3–4 дня возможен только как очень тонкий прототип; с авторизацией, routing, lifecycle и интеграционными проверками реалистичнее 4–6 дней.

### [suggestion] `turn/steer` не достижим одним Codex-адаптером

T4 обещает доставку сообщения через steer ([research:255](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:255)), но текущий ingress все сообщения во время `PROCESSING` складывает в `deferred` ([chat_state.py:139](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/chat_state.py:139)). Поэтому `CodexSession.inject()` никто не вызовет.

Для MVP проще сохранить текущую очередь и убрать steer из AC. Если mid-turn steering действительно нужен, это отдельное изменение `ChatState` с capability, обработкой отказа steer и fallback в deferred.

### [suggestion] Ручной режим безопасен для первой итерации, но не закрывает аварийную цель

Аргумент про флаппинг опровергает автоматическое переключение туда-обратно, но не односторонний sticky failover. Если требуется работа без присутствия пользователя, manual-only T7 ([research:284](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:284)) цель не выполняет.

Безопасный вариант:

1. Переключать только при terminal quota/auth failure либо после нескольких pre-output transport failures.
2. Не переключать автоматически после частичного ответа или tool side effects.
3. До commit проверять запуск альтернативного runtime.
4. Повторять исходный prompt один раз, исключая его дубликат из handoff.
5. Оставаться на Codex до ручного `/runtime claude`; никакого автоматического возврата.
6. Всегда уведомлять пользователя.

Ручная команда при этом остаётся. На автоматический аварийный режим стоит заложить ещё 2–3 дня.

### [suggestion] Переключение должно блокироваться и в `STOPPING`

План запрещает switch только при `PROCESSING`/`COMPACTING` ([research:206](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/docs/tasks/16/research-v2-runtime-switch.md:206)). Но Кеша считает `STOPPING` активной фазой и ждёт завершения stream ([chat_state.py:132](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/research-codex-harness/chat_state.py:132)). Замена runtime в этот момент может пересечься с `interrupt()` и финализацией старого batch. Новый `request_runtime()` должен выполняться под `_lock` и отклонять `PROCESSING`, `STOPPING`, `COMPACTING`.

### [suggestion] Оценка 7–10 дней оптимистична

Копирование Orchestra действительно экономит исследование JSON-RPC и event lifecycle, но не устраняет работу по интеграции. Добавились:

- session-фасад и реальный caller contract;
- безопасный MCP-мост;
- исправление transcript logging;
- durable per-runtime state и transactional switch;
- runtime-specific compact dispatch;
- либо отказ от steer, либо изменение state machine;
- аварийный failover, если он входит в цель.

Разумная плановая оценка: **10–15 инженерных дней для ручного MVP** и **12–17 дней с односторонним автоматическим failover**. В 7–10 можно попасть при удачной интеграции, но это optimistic target, не commitment.

### [question] Какова область и долговечность `/runtime`?

T2 задаёт глобальный `RUNTIME` из env, но состояние Кеши и session IDs организованы per-chat. Нужно явно решить:

- `/runtime` меняет один чат или весь бот;
- выбор переживает restart;
- `/clear` очищает только активный runtime или обе нативные сессии;
- старые runtime IDs сохраняются после `/clear`.

Для существующей архитектуры наиболее естественно: per-chat, durable, `/clear` очищает обе сессии и pending handoff.

### [question] Юридический вывод не проверялся в этом review

По ограничению на источники `research.md`, актуальные Terms и Discussion не читались. Поэтому формулировки «главный риск» и особенно «API без юридического риска» здесь не подтверждены; последнюю лучше заменить на «без выявленного риска автоматизации через подписку» после отдельной проверки официальных условий.

## Verdict

**❌ Требует доработки перед реализацией.**

Базовая архитектура с узким backend-протоколом жизнеспособна. Compact можно не включать в этот протокол, но его runtime-dispatch и общий session lifecycle всё равно придётся проектировать — скрытый контракт действительно существует. T3 оценён слишком оптимистично, handoff пока не обеспечивает заявленный возврат в старую сессию, а manual-only переключение не завершает аварийный сценарий.

Пока это запасной двигатель, который после отказа основного предлагается собрать вручную по уведомлению в Telegram — очень аварийно, конечно. 🛩️
