Ну конечно, секрет нужно одновременно скопировать дословно и вообще не копировать 🙃

## Summary

Найдено: **1 blocking**, **3 suggestions**, **1 question**. Основная архитектура убедительна, но контракт handoff сейчас содержит невыполнимое требование безопасности.

## Findings (blocking/suggestion/question)

### blocking

1. **Secret handling противоречит `RECENT VERBATIM`.**
   Промпт требует дословно копировать последние три сообщения ([research.md:457](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:457)), одновременно запрещая секреты в результате ([research.md:468](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:468)). Метрики также требуют и ноль секретов, и 100% verbatim ([research.md:563](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:563)). Если недавнее сообщение содержит токен или ключ, оба условия выполнить невозможно. Нужен явный приоритет редактирования секретов и метрика вида «verbatim, кроме заменённых секретных фрагментов».

### suggestion

1. **`context untouched` при failed compact фактически недостижимо.**
   Критерий обещает сохранить SID и контекст без изменений ([research.md:556](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:556)), но summary-запрос сначала отправляется в исходную сессию ([compact.py:67](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/compact.py:67), [compact.py:77](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/compact.py:77)). Rollback восстанавливает указатель на SID, но не удаляет уже записанный compact-turn из транскрипта. Следует обещать сохранность SID и доступность исходной сессии, явно признав дополнительный turn и возможные file side effects.

2. **`ALLOWED` ошибочно трактуется как множество chat ID.**
   Startup-дизайн предлагает армировать `ALLOWED`-чаты ([research.md:336](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:336)), однако `ALLOWED` содержит user ID ([config.py:16](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/config.py:16)), а состояния индексируются по `msg.chat.id` ([handlers.py:73](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/handlers.py:73)). В группах эти ID различаются, поэтому restart потеряет pending night check. Chat ID нужно восстанавливать из activity/session storage независимо от allowlist.

3. **Предлагаемое хранилище само себе противоречит.**
   Исследование требует отдельную upsert-запись на чат ([research.md:331](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:331)), но затем запрещает новую таблицу ([research.md:607](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:607)). Текущая БД содержит только append-only `messages` ([message_log.py:23](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/message_log.py:23)). Для плоского MVP логичнее явно разрешить маленькую таблицу `chat_activity`, не смешивая scheduler state с журналом сообщений.

### question

1. **Что именно считается durable activity для reminders?**
   Текст включает reminder activity в offline-гейт ([research.md:285](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:285)), но предлагает записывать её через `accept_entry()` ([research.md:331](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:331)). Urgent reminders обходят этот метод через `run_urgent_prompt()` ([bot.py:226](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/bot.py:226), [chat_state.py:295](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/chat_state.py:295)). Нужно определить, сдвигают ли дедлайн plain/urgent/lazy reminders и assistant completion, и назвать общий hook.

## Verdict

**REJECTED — blocking=1.** Архитектурный вывод можно сохранять, но prompt contract нельзя принимать до разрешения конфликта между verbatim continuity и запретом секретов.

Иначе это не loss-minimizing handoff, а ночная лотерея с приватным ключом вместо билета. 🎟️

## Round (2026-07-30T04:49:32Z)

Секреты вычистили из `RECENT`, но они нашли служебный вход через Markdown 🙃

## Summary

Все пять прошлых findings закрыты. Остались **1 новый blocking** и **1 suggestion**; нерешённых вопросов нет.

## Findings (blocking/suggestion/question)

### blocking

1. **Сделать secret policy глобальной, а не только для `RECENT`.**
   Typed redaction определена лишь внутри `RECENT VERBATIM` ([research.md:474](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:474)). Pre-save запрещает секреты только в `CLAUDE.md`, но не в остальных canonical notes ([research.md:427](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:427)), хотя rubric требует ноль секретов во всех новых заметках ([research.md:516](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:516)). Аналогично секрет может встретиться в command/tool outcome вне `RECENT`. Нужна одна глобальная инструкция: никогда не записывать секреты ни в какой файл и заменять secret spans typed-маркерами во всех разделах handoff.

### suggestion

1. **Определить fail-closed поведение при ошибке activity upsert.**
   Исследование закрывает crash-window введением durable hook ([research.md:340](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:340)), но не задаёт порядок и реакцию на ошибку SQLite. Если entry admitted, а upsert не выполнен, после рестарта сохранится старый timestamp и night scheduler сможет ошибочно признать недавнюю активность 55-минутным idle. Контракт должен требовать успешную durable-запись до admission либо запрещать auto-compact до следующего успешного activity write.

## Verdict

**REJECTED — blocking=1.** Предыдущий security-конфликт устранён, но запрет секретов пока не охватывает весь pre-save/handoff контракт.

Получился хороший ночной сторож, только запасной вход в хранилище заметок пока оставили открытым. 🌙

## Round (2026-07-30T04:52:26Z)

Почти идеально: остался один призрак, переживающий рестарт 😏

## Summary

Оба findings второго раунда закрыты. Новых blocking или questions нет; осталась **1 suggestion**.

## Findings (blocking/suggestion/question)

### suggestion

1. **Fail-closed latch не переживает рестарт.**
   Таблица хранит только `last_activity_utc` ([research.md:346](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:346)), а при ошибке upsert блокировка auto-compact описана только для текущего процесса ([research.md:352](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:352)). Если после ошибки и до успешной записи сервис перезапустится, startup увидит старый timestamp и снова сможет разрешить compact, вопреки acceptance-критерию ([research.md:599](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/14/research.md:599)). Стоит определить консервативное restart-поведение для такого случая.

## Verdict

**APPROVED — blocking=0.** Исследование достаточно цельное для перехода к планированию; оставшийся edge case не блокирует MVP.

Дверь уже заперта — осталось решить, куда сторож кладёт журнал после перезагрузки. 📒

## Round (2026-07-30T04:53:44Z)

Ну всё, состояние теперь переживает рестарт лучше некоторых сервисов 😏

## Summary

Последняя suggestion закрыта: `quiescent=false` фиксируется до admission и сохраняет fail-closed состояние через crash/restart. Новых findings нет.

## Findings (blocking/suggestion/question)

Нет.

## Verdict

**APPROVED — blocking=0.** Исследование готово к планированию реализации.

Ночной сторож теперь не только ведёт журнал, но и умеет его читать после обморока. 🌙
