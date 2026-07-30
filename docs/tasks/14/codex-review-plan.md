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
