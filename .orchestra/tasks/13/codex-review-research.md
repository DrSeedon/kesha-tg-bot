## Summary

Ну конечно, убрали сырую ошибку — и почти приготовили дубликат вместо неё 🙃 Остались одно blocking-упущение в Telegram terminal path и одно уточнение cache scope. Durable SID transaction и cancellation-aware progress cleanup теперь описаны достаточно.

## Findings

### blocking — Terminal limit notice не предотвращает `empty` fallback

В [research.md:243](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/research.md:243) обещано отсутствие duplicate output, но механизм не указан. Текущий `_ask_inner()` после отправки limit notice делает `break`, оставляя `parts` и `finalized` пустыми; эпилог затем отправляет `STRINGS["empty"]` ([response_stream.py:315](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:315), [response_stream.py:398](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/response_stream.py:398)). После подавления synthetic `TextBlock` это детерминированно даст два сообщения: limit notice и «пустой ответ». Research должен потребовать terminal-handled flag либо добавление ID отправленного notice в `finalized`, плюс тест обоих путей — `message.answer` и reminder через `bot.send_message`.

### suggestion — Очищать rejection cache только по сопоставимому terminal outcome

Контракт в [research.md:170](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/research.md:170) разрешает любому `RateLimitEvent(status="allowed")` очистить cache. У события есть несколько независимых `rate_limit_type` и отдельный `overage_status`, тогда как `AssistantMessage.billing_error` вообще не несёт совместимого limiter key ([официальные SDK types](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.128/src/claude_agent_sdk/types.py#L1155-L1196)). Без scope чужой `allowed` transition может снять сохранённый rejection и снова разрешить auto-compact. Проще и безопаснее сделать latch итогом завершённого turn: очищать его только успешным terminal `ResultMessage` без pending limit signal; либо сопоставлять `allowed` по source/type. Нужен тест `rejected(A) → allowed(B) → terminal error`, где auto остаётся заблокированным.

## Verdict

**REVISE.** Telegram terminal path пока нарушает заявленный invariant «одно короткое сообщение», поэтому research ещё не готов к реализации. После явного terminal-handled контракта и уточнения cache scope остальные load-bearing claims выглядят согласованными; файлы не изменялись.

Иначе бот героически победит лимит, сообщив пользователю следом, что вообще ничего не ответил 🤷

## Round (2026-07-28T06:15:03Z)

## Summary

Ну всё, третий раунд наконец без сюжетного твиста 😏 Оба предыдущих замечания исправлены.

## Findings

- **FIXED — Telegram terminal path.** `terminal_handled=True` задан для `message.answer` и reminder `bot.send_message`; `empty` fallback явно учитывает флаг ([research.md:222](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/research.md:222)).

- **FIXED — rejection latch scope.** Очистка разрешена только успешным terminal `ResultMessage` без pending limit; чужой `allowed` event не влияет ([research.md:170](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/research.md:170), [research.md:219](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/docs/tasks/13/research.md:219)).

Новых blocking/suggestion/question findings нет.

## Verdict

**APPROVED.** Оба load-bearing контракта теперь закрыты; оставшихся блокеров не найдено.

Лимит наконец получает ровно один ответ — почти неприлично нормальное поведение 🙃
