# V-42 — долгий тул обрывал ход, ответ терялся

## Причина (подтверждена кодом)
`response_stream` брал 300 с тишины после тула за зависание, показывал «stream timeout» и вызывал
`ClaudeSession.reconnect()`, который лишь выставляет `_pending_disconnect`; клиент реально
отключался при следующем `_ensure_connected`. Всё это время CLI доделывал ход (веб-поиски, commit,
финальный ответ) без получателя.

## Изменения
- `claude_session.py`: `send_message` ведёт `open_work` (tool_use главного агента ↔ ToolResultBlock в
  UserMessage; Task Started/Progress ↔ Notification/TaskUpdated с терминальным статусом) и отдаёт
  чанк `liveness`. Новый `abort_turn()`: interrupt (5 с) + дожидаемый `safe_disconnect` (15 с),
  сброс `_is_processing/_expected_results`.
- `response_stream.py`: пороги вынесены в модуль; при `open_work>0` порог `CHUNK_TIMEOUT_OPEN_WORK`=1800 с;
  на обрыве `abort_turn` вызывается ДО сообщения; Codex без `abort_turn` → прежний `reconnect()`.
  Ретрай «пустого» ответа не делается, если тул уже стартовал (повтор повторил бы эффекты);
  уведомление об обрыве уходит и при `message=None`.
- Тесты `tests/test_long_tool_liveness.py` (реальные типы SDK-сообщений): тишина 3× старого порога
  с открытым bash → ответ доставлен, interrupt/disconnect не вызывались; настоящий обрыв →
  `["interrupt","disconnect"]` до уведомления, клиент снят, поздние события не доставляются.
  Мутации (вернуть `reconnect()`; игнорировать `liveness`) краснят по одному тесту.

## Из Orchestra взято / не взято
Взято: идея «тишина ≠ смерть, долгий bash выглядит как зависший» (`app/stall_signals.py`) и учёт
событий `TaskStarted/Progress/Notification` как признака живой работы (`backend_claude.py`).
Не взято: сигнал-только-информирование без остановки (у Кеши владелец требует реальной остановки),
20-мин родительский сигнал и БД-квитанции доставки, `task_output_tail` (хвост вывода bash) — лишняя
машинерия для одного чата; мониторинг процессов CLI — в TODO.

## Проверки
Полный сьют на версиях прода (mcp 1.28.1, SDK 0.2.158): 683 passed, 3 skipped, EXIT=0 (`.orchestra/tasks/V-42/full-suite.log`).
Не проверялось на живом CLI (прод — только чтение). Ограничения — в CHANGELOG v2.9.9.
