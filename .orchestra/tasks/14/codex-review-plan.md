Саммари почти защищено — если не считать журнала, куда секрет может лечь без стука 🙃

## Summary

Найдено: **1 blocking**, **2 suggestions**, **1 question**. План в целом соответствует принятому research, но security-контракт пока не закрывает текущий debug-лог.

## Findings (blocking/suggestion/question)

### blocking

1. **Redaction должна выполняться до любого логирования.** План гарантирует очистку только перед continuation preamble и Telegram ([plan.md:214](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:214)), тогда как текущий код пишет необработанное summary в debug-лог ([compact.py:114](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/compact.py:114)). Если модель вернёт токен или private key вопреки промпту, `/debug` сохранит его в `kesha.log`. T3 должен требовать redaction сразу после сборки summary — до валидации, логов, preamble и Telegram — и проверять отсутствие raw secret в логах.

### suggestion

1. **Терминальный auto-attempt не переживает рестарт.** Таблица хранит только timestamp и `quiescent` ([plan.md:57](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:57)), но план обещает после low/unknown context, limit/error или успеха ждать новой активности ([plan.md:126](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:126)). Поскольку startup снова армирует любую строку с `quiescent=1` ([plan.md:153](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:153)), рестарт в том же idle-эпизоде повторит probe или failed compact. Нужен durable marker завершённого эпизода, очищаемый следующей активностью.

2. **Media lifecycle входит в ChatState без `begin_activity()`.** План ставит durable hook только в `accept_entry()` и `run_urgent_prompt()` ([plan.md:74](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:74)), но voice/video-note вызывают `transcription_started()` и затем напрямую `transcription_finished()` ([handlers.py:262](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/handlers.py:262), [handlers.py:290](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/handlers.py:290)). При падении во время транскрипции durable row останется старой `quiescent=1`, и после рестарта scheduler сможет ошибочно признать пользователя offline. Activity следует фиксировать до начала media work и покрыть crash/restart-тестом.

### question

1. **Как именно manual request становится sticky во время probe?** План обещает сохранить ручной `/compact` после `IDLE → COMPACTING` ([plan.md:115](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:115)), но текущий `request_compact()` в фазе `COMPACTING` сразу возвращает управление, ничего не записывая ([chat_state.py:264](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/chat_state.py:264)). Стоит явно зафиксировать изменение этого контракта и тест `manual during probe + context <20%`, иначе подтверждённая команда пользователя может исчезнуть.

## Verdict

**REJECTED — blocking=1.** Архитектура scheduler и restart-safe activity в основном убедительна, но план нельзя принимать до закрытия утечки необработанного handoff в debug-лог.

Пока это ночной сейф, который аккуратно складывает ключ под журналом охраны. 🔑

## Round (2026-07-30T05:32:31Z)

Почти приняли — но полный контекст оказался полнее плана 🙃

## Summary

Все четыре round-1 finding закрыты контрактами и тестовыми AC. Найден **1 новый blocking**; suggestions и questions нет.

## Findings (blocking/suggestion/question)

### blocking

1. **Ручной recovery использует тот же query, который уже отвергнут.** После `context_limit` план просит пользователя выполнить `/compact` ([plan.md:207](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:207)), но сохраняет task #13 flow ([plan.md:289](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:289)), где compact начинается обычным `send_message(COMPACT_PROMPT)` ([compact.py:77](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/compact.py:77)). При действительно исчерпанном контексте этот prompt может получить тот же `context_limit`, оставив пользователя в цикле `/compact` → отказ; остаётся только `/clear` с потерей контекста. Официальная документация различает интерактивные slash-команды и SDK/print queries, поэтому рекомендация Claude Code запустить `/compact` не доказывает работоспособность текущего custom-query flow ([Anthropic CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)). Нужен проверенный recovery, не требующий обычного accepted turn, и target-runtime тест: hard limit → ручной compact → успешная замена SID.

### suggestion

Нет.

### question

Нет.

## Verdict

**REJECTED — blocking=1.** Предыдущие проблемы исправлены, но обещанный daytime recovery пока может потребовать выбросить именно тот контекст, который `/compact` должен спасать.

Получился отличный аварийный выход, нарисованный на стене рядом с дверью. 🚪

## Round (2026-07-30T05:37:56Z)

Ну вот, аварийный выход наконец-то оказался настоящей дверью 😏

## Summary

Все предыдущие findings закрыты. Native manual fallback ограничен явным `/compact`, проверяет boundary и terminal Result, дренирует очередь, сохраняет SID во всех failure-сценариях и подтверждается на точном production runtime.

## Findings (blocking/suggestion/question)

Новых findings нет.

## Verdict

**APPROVED — blocking=0.** План готов к реализации; обязательные live-gates адекватно прикрывают наиболее рискованный hard-context recovery.

Теперь это уже эвакуационный план, а не квест по поиску нарисованной ручки. 🚪

## Round (2026-07-30T07:57:59Z)

Нативную slash-команду похоронили правильно, но резерв пока охраняет только первый заход 😏

## Summary

Старая native-boundary архитектура полностью удалена из плана; оставшийся `compact_boundary(trigger="auto")` используется только как invariant violation. V1 не засчитывается, v2 сохраняет 30/30.

Найдено: **1 blocking**, **3 suggestions**, **0 questions**.

## Findings (blocking/suggestion/question)

### blocking

1. **Reserve проверяется перед batch, но не перед повторными query.** План резервирует headroom один раз в `_run_batch()` ([plan.md:238](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:238)), тогда как текущий stream может выполнить до трёх `send_message(prompt)` для того же batch ([response_stream.py:307](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:307), [response_stream.py:312](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:312)). Первый attempt может уже записать tool turns в контекст, затем timeout/session error запускает retry без нового usage snapshot и съедает сохранённые 80 000 токенов. После этого custom `/compact` снова может стать невозможен. Каждый фактический retry-query должен повторно проходить authoritative reserve gate либо завершаться статическим `/compact`-then-resend outcome.

### suggestion

1. **Определить admission для новой/очищенной сессии.** Контракт требует положительный `totalTokens` и отвергает zero/unknown ([plan.md:260](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:260)), но после `/clear` SID и usage cache отсутствуют. Если target SDK до первого query возвращает `0` или `None`, бот навсегда отклонит первый новый turn. Нужен exact-runtime тест `/clear → first message` и явная безопасная семантика для `session_id is None`.

2. **Снимать reserve latch после любого успешного custom compact.** Runtime-текст очищает latch только после ручного compact или `/clear` ([plan.md:270](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:270), [plan.md:359](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:359)). Если дневной reject сменится успешным ночным auto-compact, контекст уже освобождён, но следующие сообщения останутся заблокированы. Формулировку и тесты следует распространить на successful automatic custom compact.

3. **Привязать фиксированный reserve к измеренным 64 000.** Формула жёстко использует `64_000`, но runtime validation требует от `maxOutputTokens` только корректного числового значения ([plan.md:250](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:250), [plan.md:260](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:260)). Минимально безопасно fail-closed при `maxOutputTokens != 64_000`; простая серверная смена лимита иначе обесценит измеренный reserve без смены SDK/CLI.

### question

Нет.

## Verdict

**REJECTED — blocking=1.** Revised architecture поддержана evidence и заметно проще native fallback, но retry-loop пока способен обойти её единственную гарантию сохранения manual-compact headroom.

Неприкосновенный запас хорош ровно до момента, когда второй курьер получает ключ без повторной проверки. 🔑

## Round (2026-07-30T08:04:42Z)

## Summary

Один тайный повторный запрос всё-таки выжил — классика. 🕵️ Все четыре прошлые находки закрыты: retry-preflight, fresh-session, latch-clear и invariant `64k` описаны и покрыты AC. Старых требований native `/compact`/manual boundary в плане не осталось.

## Findings (blocking/suggestion/question)

- **blocking — внутренний retry `ClaudeSession` обходит reserve-check.** План перепроверяет резерв только перед повторами в `response_stream` ([plan.md:261](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:261)), но текущий `send_message()` самостоятельно очищает SID и рекурсивно повторяет запрос при `No conversation found` или любом `exit code 1` ([claude_session.py:470](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:470)). Этот второй `query()` невидим для `response_stream`, поэтому нарушает AC «every actual retry-query», а широкая ветка `exit code 1` способна потерять пригодный durable SID. План должен явно удалить этот retry либо вернуть ошибку guarded-владельцу и проверить тестом отсутствие второго query/SID mutation.

- **suggestion:** нет.
- **question:** нет.

## Verdict

**REJECTED — blocking=1.**

Резерв охраняется исправно, кроме запасного выхода, где охранник сам выдаёт новый SID.

## Round (2026-07-30T08:07:32Z)

## Summary

Рекурсивный retry убран, зато сообщение о сломанном SID теперь стоит за дверью, которая до него не открывается. 🚪 Старый блокер закрыт локально; native slash не возвращён, остальные reserve-инварианты согласованы.

## Findings (blocking/suggestion/question)

- **blocking — stale SID зацикливается на reserve-preflight до `session_unavailable`.** Каждый обычный batch сначала вызывает `get_context_usage(refresh=True, preserve_session=True)` ([plan.md:238](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:238)). Если resume уже отвечает `No conversation found`, preflight получает unknown и выдаёт обычное «повторите позже» без latch ([plan.md:284](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:284)); до `send_message()` и его нового `session_unavailable` обработка никогда не доходит. Каждый resend повторяет тот же исход, а пользователь не получает требуемую инструкцию `/clear`. Reserve-helper должен различать stale-session connect failure и transient unknown, сохраняя SID, но завершая batch отдельным `/clear`-terminal; нужен тест именно на отказ resume во время preflight с нулём query.

- **suggestion:** нет.
- **question:** нет.

## Verdict

**REJECTED — blocking=1.**

Запасной выход предусмотрели идеально — просто поставили его за неоткрывающимся турникетом.

## Round (2026-07-30T08:12:34Z)

## Summary

Протухший SID теперь обработан правильно, но «авторитетный» замер всё ещё умеет незаметно стать вчерашним. 📉 Предыдущий блокер закрыт; native slash не возвращён.

## Findings (blocking/suggestion/question)

- **blocking — reserve-check может принять кеш вместо свежего нулевого usage.** План вызывает существующий `get_context_usage()` после подключения ([plan.md:243](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:243)), но тот при свежем `percentage == 0` возвращает `_last_ctx_usage` ([claude_session.py:640](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:640)). После выросшего контекста такой старый снимок может ошибочно разрешить следующий query и съесть manual floor. План должен требовать uncached usage для reserve-helper и тест `previous valid cache + current zero → reject, zero query`.

- **suggestion — синхронизировать outcome для unknown usage.** Основной дизайн требует retry-later без latch ([plan.md:320](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/plan.md:320)), а research-инвариант всё ещё требует `/compact`-then-resend ([research.md:717](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:717)).

- **question:** нет.

## Verdict

**REJECTED — blocking=1.**

Свежий замер запрашивают честно — просто кассир иногда подсовывает вчерашний чек.

## Round (2026-07-30T08:19:51Z)

## Summary

Похоже, кеш наконец перестал притворяться телеметрией. ✅ Все предыдущие блокеры закрыты: preflight использует uncached control response, stale SID терминализируется без query/SID mutation, hidden retries удалены, unknown outcome синхронизирован. Противоречивых native-slash требований нет.

## Findings (blocking/suggestion/question)

- **blocking:** нет.
- **suggestion:** нет.
- **question:** нет.

## Verdict

**APPROVED — blocking=0.**

Резерв теперь проверяет сегодняшний чек, а не семейную реликвию из `_last_ctx_usage`.
