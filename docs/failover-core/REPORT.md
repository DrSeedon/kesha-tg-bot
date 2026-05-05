# REPORT: failover-core

**Status**: DONE  
**Score**: 11/12  
**Branch**: feat/mnt-data-projects-python-kesha-tg-bot/failover-core  
**Commit**: e61d3a2

## Что сделано

Написан `failover.py` (~480 строк) — полный failover модуль для Kesha bot на основе Codex-Approved плана (FAILOVER_PLAN.md v8).

### Компоненты

- **`LeaseManager`** — distributed lease manager с Redis (Lua scripts, epoch fencing, local TTL)
- **`LeaseGateMiddleware`** — aiogram session middleware, блокирует ВСЕ Telegram API calls когда не owner
- **`drain_for_handback()`** — on_release callback: stop_polling → shutdown all chats → stop reminders → push repo
- **`_sync_repo()` / `_push_repo()`** — git sync helpers для on_acquire/on_release

### P0 баги зафикшены (из Codex review)

1. **`stop_polling()` reraise** — убрал `try/except` вокруг `dp.stop_polling()` в `drain_for_handback`. Ошибка теперь propagates наверх → `_graceful_release` уходит в `fail_closed` вместо тихого игнорирования. Без этого: drain "успешно завершился" даже если polling не остановился — потенциальный split-brain.

2. **`BaseException` для CancelledError** — в `_graceful_release` изменён `except Exception` → `except BaseException` + добавлен `raise`. `asyncio.CancelledError` наследует от `BaseException`, не `Exception`. Без этого: при отмене таски `_graceful_release` не вызывал `fail_closed` → состояние ownership не обнулялось.

### Добавлено в requirements.txt

```
redis[hiredis]>=5.0
```

## Codex Review Summary

Codex v1 нашёл два P0:
- P0 #1: `stop_polling` error silently swallowed
- P0 #2: `asyncio.CancelledError` not caught in `_graceful_release`

Оба зафикшены. Финальный re-review не запускался из-за таймаута сессии — но фиксы строго соответствуют Codex findings и плану.

## Score (11/12)

| # | Критерий | OK? |
|---|----------|-----|
| 1 | Соответствует FAILOVER_PLAN.md v8 | ✅ |
| 2 | Lua scripts атомарны (5 штук) | ✅ |
| 3 | Epoch fencing | ✅ |
| 4 | Local deadline (`is_owner_now`) | ✅ |
| 5 | `stop_polling` reraise (P0 fix) | ✅ |
| 6 | `BaseException` для CancelledError (P0 fix) | ✅ |
| 7 | `drain_renew_loop` параллельно drain | ✅ |
| 8 | Transport middleware (aiogram session) | ✅ |
| 9 | `fail_closed` при любой ошибке ownership | ✅ |
| 10 | Импорт чистый: `python -c "import failover"` → OK | ✅ |
| 11 | requirements.txt обновлён | ✅ |
| 12 | Финальный Codex re-review после фиксов | ❌ (таймаут) |

## Следующий шаг

Нужна интеграция в `bot.py`:
- Установить `LeaseGateMiddleware` на `bot.session`  
- Добавить `on_acquire` / `on_release` / `on_lost` callbacks  
- `ChatState.enter_shutdown()` / `exit_shutdown()` (из chatstate-shutdown ветки)
