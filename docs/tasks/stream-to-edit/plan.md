# Plan: SendMessageDraft → edit_message_text

## Цель
Заменить `SendMessageDraft` стриминг на `send_message` + `edit_message_text` чтобы юзер мог вводить текст пока бот отвечает.

## Затронутые файлы
| Файл | Изменения |
|------|-----------|
| `response_stream.py` | Основные изменения (удалить `_draft_update`, добавить `_edit_update`, изменить состояние) |
| `telegram_io.py` | Удалить `_next_draft_id()` и `_draft_counter` (больше не нужны) |
| `bot.py` | Без изменений |
| `tool_status.py` | Без изменений (уже использует edit_message_text) |

---

## Шаг 1: Новое состояние в `_ask_inner`

**Удалить переменные:**
```python
# УДАЛИТЬ:
draft_id = _next_draft_id()
last_draft_time = 0.0
last_draft_text = ""
draft_has_text = False
flood_cooldown_until = 0.0
```

**Добавить переменные:**
```python
# ДОБАВИТЬ:
current_msg_id: Optional[int] = None  # ID сообщения которое сейчас редактируем
stream_text = ""                       # накопленный текст текущего сегмента
last_edit_time = 0.0                   # время последнего edit (rate limiting)
last_edit_text = ""                    # последний отправленный текст (дедуп)
edit_flood_until = 0.0                 # flood control cooldown
```

**Изменить импорт:**
```python
# УДАЛИТЬ из импортов:
from aiogram.methods import SendMessageDraft
from telegram_io import _next_draft_id, ...

# _next_draft_id больше не нужен
from telegram_io import _send_safe, split_msg, typing_loop
```

---

## Шаг 2: Заменить `_draft_update()` на `_edit_update()`

**Константа** (рядом с `STREAM_DRAFT_INTERVAL`):
```python
STREAM_EDIT_INTERVAL = 1.0  # TG лимит ~20 edits/min ≈ 1 edit/3s, но 1s безопаснее всего
```

**Новая функция** (заменяет `_draft_update`):
```python
async def _edit_update():
    nonlocal current_msg_id, stream_text, last_edit_time, last_edit_text, edit_flood_until

    text = "".join(parts)
    if not text:
        return

    now = time.time()
    if now < edit_flood_until:
        return
    if (now - last_edit_time) < STREAM_EDIT_INTERVAL:
        return
    if text == last_edit_text:
        return

    # Нужно отправить первое сообщение
    if current_msg_id is None:
        try:
            m = await _answer(text, parse_mode=None)
            if m:
                current_msg_id = m.message_id
                last_edit_text = text
                last_edit_time = now
        except Exception as e:
            logger.debug(f"Edit stream: initial send failed: {e}")
        return

    # Редактируем существующее
    try:
        await _bot.edit_message_text(
            text[:TG_MSG_LIMIT], chat_id=cid, message_id=current_msg_id, parse_mode=None
        )
        last_edit_text = text
        last_edit_time = now
    except Exception as e:
        err = str(e)
        if "Flood control" in err or "retry after" in err.lower():
            import re
            m = re.search(r'retry after (\d+)', err, re.IGNORECASE)
            wait_sec = int(m.group(1)) if m else 30
            edit_flood_until = now + wait_sec + 1
            logger.info(f"Edit flood control, pausing for {wait_sec}s")
        elif "message is not modified" in err:
            last_edit_text = text
            last_edit_time = now
        else:
            logger.debug(f"Edit update error: {e}")
```

---

## Шаг 3: Изменить `_finalize_text_block()`

Текущая функция очищает draft (`SendMessageDraft(..., text="")`). В новом варианте:

**Что убираем:**
```python
# УДАЛИТЬ эти строки:
if draft_has_text:
    with contextlib.suppress(Exception):
        await _bot(SendMessageDraft(chat_id=cid, draft_id=draft_id, text=""))
    draft_has_text = False
```

**Что меняем в конце функции** (сброс состояния):
```python
# БЫЛО:
draft_id = _next_draft_id()
last_draft_time = 0.0
last_draft_text = ""
parts = []
has_deltas = False

# СТАЛО:
current_msg_id = None  # следующий сегмент = новое сообщение
last_edit_time = 0.0
last_edit_text = ""
stream_text = ""
parts = []
has_deltas = False
```

**Важно:** `_finalize_text_block()` уже корректно обрабатывает разбивку на чанки и финальную отправку через `_answer()`. НЕ нужно её дублировать — она посылает финальную версию с `telegramify_markdown` entities. После этого `current_msg_id = None` сигнализирует что следующий edit-update начнёт новое сообщение.

---

## Шаг 4: Вызовы `_draft_update()` → `_edit_update()`

В `_ask_inner`, в двух местах:
```python
# БЫЛО:
elif ct == "text_delta":
    ...
    await _draft_update()
elif ct == "text" and not has_deltas:
    ...
    await _draft_update()

# СТАЛО:
elif ct == "text_delta":
    ...
    await _edit_update()
elif ct == "text" and not has_deltas:
    ...
    await _edit_update()
```

---

## Шаг 5: Убрать `_draft_update(final=True)` из пути `turn_done`

В текущем коде `_finalize_text_block()` сама финализирует текст через `_answer()`. Больше не нужен финальный `_draft_update(final=True)` — он не существовал как явный вызов, но `_finalize_text_block()` должна теперь "перезаписать" текущий edit-message через `_answer()`.

**Проблема:** При `_finalize_text_block()` сейчас вызывается `_answer()` — это создаёт НОВОЕ сообщение. Но у нас уже есть `current_msg_id` с live-текстом. Нужно сделать финальный edit на него, а не посылать новое сообщение.

**Решение:** В `_finalize_text_block()` проверить `current_msg_id`:

```python
async def _finalize_text_block():
    nonlocal parts, has_deltas, current_msg_id, last_edit_time, last_edit_text, stream_text

    raw = "".join(parts)
    if not raw:
        return
    await _stop_typer(typer)

    try:
        converted_text, entities = _md_convert(raw)
        chunks = _split_entities(converted_text, entities, TG_MSG_LIMIT)
    except Exception as e:
        logger.warning(f"telegramify_markdown convert failed: {e}, sending plain")
        chunks = [(p, []) for p in split_msg(raw)]

    from aiogram.exceptions import TelegramRetryAfter

    for i, (chunk_text, chunk_ents) in enumerate(chunks):
        if not chunk_text:
            continue
        ent_dicts = [e.to_dict() for e in chunk_ents] if chunk_ents else None

        # Первый чанк — редактируем live-сообщение если оно есть
        if i == 0 and current_msg_id is not None:
            for attempt in range(3):
                try:
                    await _bot.edit_message_text(
                        chunk_text, chat_id=cid, message_id=current_msg_id,
                        parse_mode=None, entities=ent_dicts
                    )
                    finalized.append(current_msg_id)
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception as e:
                    err = str(e)
                    if "message is not modified" in err:
                        finalized.append(current_msg_id)
                    else:
                        logger.error(f"_finalize edit error: {e}")
                        # Fallback: отправить новое сообщение
                        try:
                            m = await _answer(chunk_text, parse_mode=None, entities=ent_dicts)
                            if m:
                                finalized.append(m.message_id)
                        except Exception:
                            pass
                    break
        else:
            # Последующие чанки (overflow > 4096) — новые сообщения
            for attempt in range(3):
                try:
                    m = await _answer(chunk_text, parse_mode=None, entities=ent_dicts)
                    if m:
                        finalized.append(m.message_id)
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception as e:
                    logger.error(f"_finalize_text_block error: {e}")
                    try:
                        m = await _answer(chunk_text, parse_mode=None)
                        if m:
                            finalized.append(m.message_id)
                    except Exception:
                        pass
                    break

    current_msg_id = None
    last_edit_time = 0.0
    last_edit_text = ""
    stream_text = ""
    parts = []
    has_deltas = False
```

---

## Шаг 6: Убрать ненужное из telegram_io.py

```python
# УДАЛИТЬ:
_draft_counter = 0

def _next_draft_id() -> int:
    global _draft_counter
    _draft_counter += 1
    return _draft_counter
```

И убрать импорт `_next_draft_id` в `response_stream.py`.

---

## Итоговый diff по переменным

| Было | Стало |
|------|-------|
| `draft_id` | удалено |
| `last_draft_time` | `last_edit_time` |
| `last_draft_text` | `last_edit_text` |
| `draft_has_text` | `current_msg_id` (None если нет активного сообщения) |
| `flood_cooldown_until` | `edit_flood_until` |
| `STREAM_DRAFT_INTERVAL = 0.3` | `STREAM_EDIT_INTERVAL = 1.0` |
| `_draft_update()` | `_edit_update()` |
| `SendMessageDraft` import | удалено |
| `_next_draft_id()` import | удалено |

---

## Edge Cases и их обработка

### EC-1: Flood control при edit
Точно такой же механизм как в `ToolStatusTracker`: `edit_flood_until = now + wait_sec + 1`. Edits пропускаются, но стриминг в parts продолжается.

### EC-2: Parse error при финализации
`_finalize_text_block()` уже обрабатывает: `telegramify_markdown` → entities, fallback на `parse_mode=None`. Теперь добавляется ещё один уровень: если `edit_message_text` фейлится с `can't parse entities` → повтор без entities.

### EC-3: Текст > 4096 во время стриминга
В `_edit_update()`: текст обрезается `[:TG_MSG_LIMIT]`. Это временная мера — пользователь видит первые 4096 символов в live режиме.
При финализации `_finalize_text_block()` делает правильный `_split_entities()` и отправляет все чанки.
Вопрос: нужно ли также split в live режиме? **Нет** — draft тоже обрезал по `TG_MSG_LIMIT`. Финализация всё исправит.

### EC-4: Reminder turns (message=None)
`_answer()` фолбэчит на `_bot.send_message(cid, ...)`. Первый вызов `_edit_update()` когда `current_msg_id is None` → вызовет `_answer()` → всё работает.

### EC-5: Stop в середине стриминга
При `cs.should_stop()`:
```python
if parts:
    parts.append("\n\n_(stopped)_")
    break
```
После этого `_finalize_text_block()` вызовется и корректно финализирует через edit или fallback.

### EC-6: Retry при ошибке (reconnect)
При retry: `parts.clear()`, `has_deltas = False`. Нужно также сбросить `current_msg_id = None` — иначе при retry попытаемся редактировать старое (возможно несвежее) сообщение.

Добавить в retry cleanup:
```python
parts.clear()
has_deltas = False
current_msg_id = None  # ДОБАВИТЬ
last_edit_text = ""    # ДОБАВИТЬ
```

### EC-7: `_finalize_text_block` с пустым `current_msg_id`
Если вызвана до первого `_edit_update()` (Claude сразу использовал тул без текста), то `current_msg_id = None` и функция вернёт без попыток edit. Это правильное поведение — текста нет.

### EC-8: "message is not modified" при финализации
При финализации через `edit_message_text`: если текст не изменился с последнего live-edit → TG вернёт "message is not modified". Обработать как success (добавить `current_msg_id` в `finalized`).

---

## Риски

### R-1: Edit rate limit
TG лимит — ~1 edit/сек на сообщение. С `STREAM_EDIT_INTERVAL = 1.0` мы уже близко к лимиту. Можно поднять до 1.5-2.0 если flood errors будут частыми.

**Митигация:** `edit_flood_until` механизм (уже в плане). Stale edits пропускаются.

### R-2: Видимость live-текста
При draft пользователь видел текст "вырастающим" с высокой частотой (0.3s). При edit — обновление каждые 1s. Это заметное снижение "живости" анимации.

**Трейдофф:** Принято сознательно — взамен получаем возможность вводить текст.

### R-3: Первое сообщение появляется после накопления первого чанка
С draft первый текст появлялся мгновенно при первом символе. С edit — первое `send_message()` происходит при первом `_edit_update()`, который тоже throttled на 1.0s.

**Решение:** В `_edit_update()` при `current_msg_id is None` — НЕ применять throttle (только дедуп по пустому тексту). Первое сообщение отправляем сразу.

```python
# При current_msg_id is None — не проверяем rate limit, только пустой текст
if current_msg_id is None:
    if not text:
        return
    try:
        m = await _answer(text, parse_mode=None)
        ...
    ...
    return
# Для edit — применяем throttle
if now < edit_flood_until:
    return
if (now - last_edit_time) < STREAM_EDIT_INTERVAL:
    return
```

### R-4: Concurrent _finalize_text_block + _edit_update
Обе функции — `async`, выполняются в одном event loop. Нет race conditions — всё sequential. OK.

### R-5: current_msg_id потерян при retry
Если после reconnect мы пробуем редактировать старое сообщение — оно из предыдущей попытки. Нужно сбросить `current_msg_id = None` при retry. Уже учтено в EC-6.

---

## Порядок имплементации

1. Убрать переменные `draft_*`, добавить `current_msg_id` + `edit_*` — чистое переименование
2. Написать `_edit_update()` с логикой send/edit/flood-control
3. Заменить вызовы `_draft_update()` → `_edit_update()`
4. Переписать `_finalize_text_block()` — первый чанк edit, остальные send
5. Почистить retry blocks (добавить `current_msg_id = None`)
6. Удалить `_next_draft_id()` из telegram_io.py
7. Почистить импорты (SendMessageDraft, _next_draft_id)
8. Тест: отправить длинный промпт → убедиться что текст появляется и редактируется; отправить сообщение пока бот печатает → убедиться что Telegram НЕ блокирует ввод

---

## Что НЕ меняется

- `ToolStatusTracker` (tool_status.py) — уже использует edit_message_text
- `typing_loop` — без изменений
- `_finalize_status()` — без изменений
- `message_log.log_assistant()` hook — без изменений
- Retry и reconnect логика — без изменений (кроме EC-6 cleanup)
- `telegramify_markdown` конвертация — без изменений
- `_send_safe()` — без изменений
- `bot.py` wiring — без изменений
