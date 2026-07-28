## Summary

Ну да, «атомарность» снова началась после опасного места 😏 Обнаружены две блокирующие гонки с потерей пользовательского сообщения или активного контекста. Полный pytest не запускался согласно запросу; заявленные 84 passing tests эти сценарии не покрывают.

## Findings

1. **blocking: Revalidate an injection after the awaited query**  
   [claude_session.py:397](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/claude_session.py:397) — Confidence: 0.97

   Пока `inject()` ожидает `_client.query(text)` под `_query_lock`, основной receive-loop может получить исключение и очистить `_client`/`_connected` на строках 365–367 или 382–383 без этого lock. После возобновления `inject()` всё равно увеличивает `_expected_results` и возвращает `True`; `ChatState` считает сообщение принятым и не ставит его обратно в очередь, хотя stream уже завершается и результат никто не прочитает. Это реальная потеря сообщения при совпадении inject с ошибкой SDK; состояние соединения нужно повторно проверить после `await query()` либо атомарно закрывать processing относительно in-flight inject.

2. **blocking: Protect the original session before requesting the summary**  
   [compact.py:133](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/upgrade-claude5/compact.py:133) — Confidence: 0.94

   Транзакция сохраняет session ID только после завершения summary-turn. Но `send_message(COMPACT_PROMPT)` при транзиентном `exit code 1` вызывает `_invalidate_session()`, сразу записывает пустой ID на диск и повторяет запрос в новой сессии. Поэтому при последующей ошибке compact сообщает «контекст сохранён», хотя старый ID уже потерян; при успешном retry он может закоммитить саммари, созданное без прежнего контекста. Исходный persisted ID должен быть защищён до summary-turn либо summary-запросу нельзя позволять необратимо инвалидировать его.

## Verdict

**REJECT**

Обе находки являются blocking по заданной шкале: первая теряет принятое пользовательское сообщение, вторая может необратимо потерять активный контекст. Атомарность после summary — примерно как пристегнуться уже после столкновения.

## Round (2026-07-28T07:22:28Z)

## Summary

Надо же, транзакция теперь действительно транзакция 😏

- Prior blocker 1 — **FIXED**. `inject()` повторно проверяет тот же client/connection/processing после `await query()`, поэтому не подтверждает потерянную инъекцию; generic failure также сбрасывает `_expected_results`.
- Prior blocker 2 — **FIXED**. Старый SID защищён до summary, промежуточные durable writes подавлены, смена source SID прерывает compact с rollback, а candidate отделяется только после успешного summary.
- Новых blocking correctness/data-loss/race дефектов не найдено.
- Полный pytest не перезапускался; проверены runtime-дифф и соответствующие новые тестовые сценарии. Учтены заявленные 39 focused и 86 full passing tests.

## Findings

Нет blocking, suggestion или question findings.

## Verdict

**APPROVED**

Оба прежних блокера закрыты, blocking findings: **0**. Теперь ремень всё-таки застегнули до поездки.
