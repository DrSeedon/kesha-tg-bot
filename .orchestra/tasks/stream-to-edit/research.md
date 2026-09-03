# Research: SendMessageDraft → edit_message_text migration

## Проблема
`SendMessageDraft` (Bot API 9.5) блокирует ввод пользователя пока бот "печатает". Юзер не может отправить новое сообщение во время стриминга — Telegram блокирует текстовое поле чата.

## Текущая архитектура стриминга

### Ключевые переменные состояния (`_ask_inner`)
```
parts: list[str]          # текущие чанки текста (сбрасываются при _finalize_text_block)
has_deltas: bool          # получили ли хоть один text_delta (для дедупа с "text")
draft_id: int             # ID дraftа (возвращается _next_draft_id())
last_draft_time: float    # время последнего draft update
last_draft_text: str      # последний отправленный текст (для дедупа)
draft_has_text: bool      # флаг — есть ли активный draft с текстом
flood_cooldown_until: float  # до какого времени НЕ отправлять draft updates (flood control)
finalized: list[int]      # message_ids всех финализированных сообщений
status: ToolStatusTracker # текущий тул-статус трекер (или None)
```

### Flow текущего стриминга

**Чанк `text_delta`:**
1. `parts.append(chunk["content"])`
2. `await _draft_update()` — отправляет `SendMessageDraft` с throttling (0.3s interval)

**`_draft_update()`:**
- Rate limit: 0.3s между updates, skip если текст не изменился
- Flood control: если TG вернул flood error — пауза на `retry_after + 1s`
- `final=True`: отправляет с `parse_mode="Markdown"`, fallback на plain text при parse error
- Использует `draft_id` для привязки к конкретному draft

**`_finalize_text_block()`:** вызывается когда:
- Появился tool call (`ct == "tool"`)
- `turn_done`
- После цикла, если `parts` непусты

Что делает:
1. Отменяет typer
2. Очищает draft: `SendMessageDraft(..., text="")` — "гасит" анимацию
3. Конвертирует raw text → entities через `telegramify_markdown.convert()`
4. Разбивает на чанки через `_split_entities()` (уважает entity boundaries)
5. Отправляет каждый чанк через `_answer()` с retry при FloodWait
6. Добавляет `message_id` в `finalized[]`
7. Сбрасывает `parts, has_deltas, draft_id, last_draft_time, last_draft_text`

### ToolStatusTracker — ОТДЕЛЬНЫЙ механизм
- Использует `bot.send_message()` → `bot.edit_message_text()` (уже классический edit!)
- НЕ использует SendMessageDraft
- Имеет собственный flood control и rate limiting (1s interval)
- `finalize()` — рендерит финальное состояние (все ✅) и возвращает `message_id`
- `cancel_empty()` — удаляет сообщение если не было тулов (edge case)

### Chunk types
- `text_delta` — инкрементальный текст (основной поток)
- `text` — полный текст (если нет deltas — edge case для некоторых ответов)
- `tool` — вызов инструмента
- `turn_done` — конец хода
- `error` — ошибка сессии (с retry логикой)

### Retry logic
- `MAX_RETRIES = 2` (из config.py)
- При `error` чанке с "session"/"process" в тексте → reconnect + retry
- При stream timeout (120s text / 300s tool) → reconnect + retry
- При outer exception → reconnect + retry

## Edge Cases

### 1. Flood Control (draft_update)
Текущий механизм в `_draft_update()`:
```python
elif "Flood control" in err_str or "retry after" in err_str.lower():
    flood_cooldown_until = now + wait_sec + 1
```
После перехода на edit_message_text: TG лимит 1 edit/сек на одно сообщение → нужен аналогичный rate limit.

### 2. Parse error при финализации
В `_finalize_text_block()`:
- Попытка 1: `telegramify_markdown.convert()` → entities
- Fallback 1: при `can't parse entities` → plain text
- Fallback 2: при любой ошибке → `_answer(chunk_text, parse_mode=None)`

В новом подходе edit_message должен иметь такой же fallback.

### 3. Превышение лимита 4096 символов
Текущий подход: `draft_update()` обрезает по `TG_MSG_LIMIT`, при финализации делает `_split_entities()`.
В новом подходе: при накоплении текста > 4096 — нужно финализировать текущее сообщение и начать новое.

### 4. Multi-message responses
`finalized` список собирает все `message_id`. После перехода: каждый "сегмент" ≥ 4096 → новое сообщение.

### 5. Reminder turns (message=None)
`_answer()` fallback на `_bot.send_message()` когда `message is None`. Нужно сохранить эту логику для первого сообщения.

### 6. message_log hook
В конце `_ask_inner()`:
```python
_get_msg_db().log_assistant(cid, text)
```
Это логирует ПОЛНЫЙ текст ответа (все parts до сброса). Нужно сохранить: логировать `"".join(all_parts)` после всего.

### 7. `has_deltas` флаг
Защищает от дублирования когда одновременно приходит `text_delta` и `text`. При наличии deltas — чанк `text` игнорируется. Нужно сохранить.

### 8. `_next_draft_id()` в telegram_io.py
Глобальный счётчик для уникальных draft ID — после миграции становится ненужным. Можно удалить.

### 9. `draft_has_text` флаг
Нужен для корректной очистки draft перед финализацией. При новом подходе — аналогом будет `current_msg_id` (ID сообщения которое сейчас редактируется).

### 10. Typing indicator
`typing_loop()` отправляет `TYPING` action каждые 4 секунды. Текущий flow: `_stop_typer()` вызывается при `_finalize_text_block()`. 
При новом подходе: продолжать показывать typing пока идёт стриминг, остановить только после финальной финализации.

## Что удаляется
- `SendMessageDraft` import из response_stream.py
- `draft_id`, `last_draft_time`, `last_draft_text`, `draft_has_text`, `flood_cooldown_until` variables
- `_draft_update()` функция
- `_next_draft_id()` в telegram_io.py (если нигде больше не используется)
- `from aiogram.methods import SendMessageDraft` import

## Что остаётся
- `_finalize_text_block()` — логика конвертации markdown + split_entities (полностью переиспользуется)
- `typing_loop` — остаётся как есть
- Flood control handling — переносится на `edit_message_text`
- `message_log` hook — без изменений
- `ToolStatusTracker` — без изменений (уже использует edit_message_text)
- Retry logic — без изменений
- `has_deltas` flag — без изменений

## TG API ограничения для edit_message_text
- Лимит: ~20 edits/min на одно сообщение (практически ~1/сек без flood errors)
- Нельзя редактировать сообщение > 48h
- Нельзя редактировать не-текстовые сообщения
- Ошибка "message is not modified" — нормальная (текст не изменился)
- TelegramRetryAfter при flood — нужен обработчик

## Вывод
Основные изменения — только в `response_stream.py`:
- Заменить `_draft_update()` на `_edit_update()` с `bot.edit_message_text()`
- При первом чанке текста — `bot.send_message()` → сохранить `current_msg_id`
- При превышении 4096 — финализировать текущее (через `_finalize_text_block()`), `send_message()` → новый `current_msg_id`
- Убрать `draft_id`, добавить `current_msg_id`

ToolStatusTracker и finalize логика не меняются.
