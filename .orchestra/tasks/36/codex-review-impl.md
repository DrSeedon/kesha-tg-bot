# #36 — implementation review journal

Route: high-risk floor (shared CLI process/session lifecycle, phase gate + asyncio lock)
→ Sol technically desirable, no explicit Sol authorization → one Luna pass planned.

- Attempt 1 — 2026-09-06, `codex_review(mode="implementation", model="gpt5.6luna")` →
  `invalid_argument: implementation review requires a clean committed worktree`
  (этот журнал был не закоммичен). Ревьюер не отвечал — раунд не потрачен.
- Attempt 2 — 2026-09-06, тот же вызов на чистом дереве →
  `weekly_quota_blocked: Codex quota is 99% — at or above the hard stop 99%`.
  Отказ ИНСТРУМЕНТА, ревьюер не отвечал — раунд не потрачен.

**Итог: `Review: none — Codex unavailable`.** Замена ревьюеру не поднималась (правило скилла).
Вместо ревью — собственный adversarial self-review; проверенное ниже, а не «выглядит нормально».

## Self-review: что проверено кодом, а не рассуждением

1. **Латч фазы.** `reload_cli` отпускает чат через `finally: _finish_cancel_safely(_finish_reload())`
   — один вызов на всех путях (успех, `except Exception`, `CancelledError`). `except Exception`
   НЕ ловит `CancelledError` (BaseException), поэтому отмена пробрасывается, а уборка всё равно
   доигрывает — ровно как в `switch_runtime`. Мутация M8 (снять `finally`) красит
   `test_reload_failure_does_not_latch_the_chat`.
2. **Двойной дренаж.** `_drain_or_idle` вызывается ровно из `_finish_reload`; на успешном пути
   второго вызова нет. `_runtime_switch_task` переиспользован намеренно: `ChatRegistry.shutdown`
   отменяет именно его, иначе зависший спавн CLI держал бы остановку бота.
3. **Конкурирующие команды во время релоада:** `/clear` → `request_clear` видит PROCESSING и
   отказывает (`clear_busy`); `/compact` → `compact_requested` копится и отрабатывает обычной
   машинерией хода; сообщение/напоминалка → `deferred` и ответ после релоада (тест
   `test_a_message_sent_during_the_reload_is_answered_afterwards`, мутация M9).
4. **Состояние сессии после ПРОВАЛА релоада.** Claude: `session_id` не тронут (`preserve_session=True`,
   мутация M1), клиент снят → следующее сообщение поднимет CLI лениво через `_ensure_connected`.
   Codex: `_connect` при любом исключении сам делает `_teardown_process()` (`except BaseException`
   в `_connect_locked`), процесс не остаётся сиротой.
5. **Подмена `kesha` с диска невозможна:** `_load_global_mcp` кладёт in-process сервер ПЕРВЫМ и
   добавляет диск через `setdefault`. Порядок сохранён с прежней версии (там был `if name not in`).
6. **Недописанный/битый `.mcp.json`** → `JSONDecodeError` → `logger.warning` с путём, источник
   пропускается, остальные читаются. Не тихо (мутация M6).
7. **Markdown в ответе.** Имя сервера с `_` ломает Markdown V1, но `telegram_io._send_safe:136`
   ловит `can't parse entities` и переотправляет тем же текстом с `parse_mode=None` — ответ
   не теряется. Проверено чтением, не предположением.
8. **`_REQUIRED_METHODS` += `apply_mcp_servers`:** оба боевых адаптера реализуют, фейки в
   `test_runtime_registry` наследуют `ClaudeSession` либо падают намеренно. 636 passed.
9. **Осиротевший вызов:** `.orchestra/tasks/19/probe_argv.py` (боевой acceptance-пробник #19)
   конструировал `ChatRegistry(mcp_config=...)` и читал `bot._mcp_config` — сломан моей сменой
   сигнатуры, починен на `mcp_loader` / `_load_global_mcp()`.

## Осталось непроверенным

- Боевой `/reload` на проде не запускался (воркер не деплоит): доказан контракт
  (`options.resume`, содержимое внешнего `--mcp-config`), не факт запуска реального сервера.
- Codex-ветка на заглушках `_connect`/`_teardown_process`; живой `app-server` не поднимался (квота).
