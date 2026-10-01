# V-41 — отказ safeguards: вырезать ход из памяти, не ретраить

Исполнитель: оркестратор (spawn воркеров заблокирован гейтом квоты: Claude 38% > 34.78%, Codex 88% > 86.08%).

## Инцидент 01.10.2026, чат 720740564

11:43 «изучи … быть белым хакером» + пересланное фото → CLI 2.1.280: `system/model_refusal_no_fallback`
(`apiRefusalCategory=cyber`, `refusedUserMessageUuid=0e750b11…`), синтетический assistant
`<synthetic>` с `error=invalid_request` и текстом `API Error: Opus 5.5 (1M context)'s safeguards flagged
this session …`, `Result: stop=refusal`. Перед этим CLI сам делает одну попытку
(`system/informational` «safeguards stopped the response above · continuing once» + isMeta-сообщение).

Бот: текст содержит «session» → ветка `Session error, reconnecting` в `response_stream.py` → 2 реконнекта
в ТУ ЖЕ сессию; каждая попытка дописала в transcript ещё одну копию вопроса (цепочка
`user → … → synthetic error → user (повтор)`). Итог: 3 копии отклонённого текста в контексте.
Дальше отравление: компакт 11:44 — `stop=refusal`, `summary_error`; безобидное «сохрани в мд» —
сначала `stop=refusal`, на реконнекте два отказа посреди хода, ответ всё же дошёл (`end_turn`) с
текстом API Error внутри; компакт 11:46 — `invalid_summary`. Ранее такие же отказы были в сессиях
от 13.09, 17.09, 28.09 (grep `safeguards flagged` по transcript'ам).

## Что сделано

- `claude_session.py`: `model_refusal_no_fallback` запоминается; синтетический assistant с `error`
  после него не уходит в чат; терминальный результат (`stop_reason="refusal"` или `is_error`) →
  после освобождения клиента `_rollback_refused_turn`: найти `parentUuid` отклонённого сообщения в
  transcript, `fork_session(up_to_message_id=parent)`, записать новый id в файл сессии, обнулить
  базу учёта (форк не несёт `cost-state`). Нет родителя → новая сессия. Во время транзакции
  компакта откат не выполняется (только отключение клиента). Чанк `kind="safety_refusal"`.
- `response_stream.py`: `safety_refusal` обрабатывается до usage/context/«session»-веток, одно
  сообщение пользователю, без ретрая.
- `_transcript_path` теперь повторяет правило CLI для каталога проекта (`[^a-zA-Z0-9]` → `-`);
  прежний `replace(os.sep, "-")` расходился с CLI на путях с `_` (на проде `/opt/cog-second-brain`
  совпадал). Тест учёта базы, копировавший старое правило, выровнен.

## Проверки

- Живой форк на копии реального transcript (`dd9522b4…`, 18 МБ) на проде во временном
  `CLAUDE_CONFIG_DIR`: форк до `1f6d10da` → 1613 записей, последний — ответ «🔬 Сверил пост канала…»,
  ни текста вопроса, ни «safeguards flagged». Модель на форке не запускалась.
- Тесты: `tests/test_safety_refusal.py` (форма потока с прода; откат + resume форка; первое
  сообщение → новая сессия; отказ внутри завершённого хода не откатывает),
  `tests/test_response_limit.py::test_safety_refusal_is_one_notice_without_reconnect`.
- Сьют и мутации: `full-suite.log`.

## Не сделано / ограничения

- Отказ посреди хода, закончившегося `end_turn`, не откатывается.
- `message_log`/RAG хранит сообщение — вырезается только контекст модели.
- Живого отказа после деплоя ещё не было.
