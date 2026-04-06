# Kesha TG Bot

Telegram-бот — обёртка над Claude Code CLI через официальный `claude-agent-sdk`.

## Архитектура

```
Telegram (Aiogram 3) → bot.py → claude_session.py → claude-agent-sdk → claude CLI → OAuth
```

- **bot.py** — handlers, дебаунс, i18n, стриминг, media cache, album support, tool display
- **claude_session.py** — обёртка над `query()` с `resume`, `include_partial_messages`, rate limit, cost tracking
- **kesha_tools.py** — MCP tools для самонастройки и отправки медиа (send_photo, send_file, schedule_message)
- **system_prompt.txt** — system prompt для Claude (TG контекст, форматирование, self-config)
- **setup_wizard.py** — интерактивная настройка .env при первом запуске

## Сессии

- Первое сообщение → `query()` без resume → новая сессия → `session_id` сохраняется в `./storage/session_id`
- Следующие → `resume=session_id` → контекст сохраняется
- `/clear` → сброс → новая сессия
- Session переживает рестарт бота (persistent file)

## Формат медиа в промптах

- `[photo: path]` + caption на отдельной строке
- `[voice: path | расшифровка]`
- `[video_note: path | расшифровка]` (ffmpeg → Deepgram)
- `[document: path (filename)]` + caption
- `[video: path]`, `[audio: path (name)]`, `[sticker: emoji]`
- Форварды: `[Forwarded from Name]`
- Реплаи: `[reply: "цитата"]`
- Альбомы: `aiogram-media-group` группирует фото/видео в один блок

## Стриминг

- `include_partial_messages=True` → `StreamEvent` с `text_delta`
- Сообщение создаётся при первом чанке, edit каждые 1.5 сек
- Tool calls показываются в стриме: `🔧 Read /path`, `🔧 Bash ls`
- Финальный edit с Markdown, fallback на plain text
- Текст до tool call сбрасывается (мысли вслух)

## Дебаунс и очередь

- Сообщение → N сек ожидание (по умолчанию 3, `/debounce`) → склейка в батч
- Батч: `--- message 1/N ---` разделители
- Пока Claude занят → очередь → merge всех при освобождении → один ответ
- `[Имя Фамилия (@username)]:` перед каждым промптом

## MCP Tools (kesha)

- `set_model` — сменить модель
- `set_debounce` — задержка склейки
- `toggle_debug` — debug логи
- `get_bot_status` — полный статус (model, session, rate limit, cost, uptime)
- `restart_bot` — перезапуск через systemd
- `send_photo` — отправить фото в ТГ
- `send_file` — отправить файл в ТГ
- `schedule_message` — отложенное сообщение (1-86400 сек)

## Media

- Хранение: `./storage/media/` с автоочисткой (24ч)
- Имена: `photo_20260406_163000_1234.jpg` (тип_дата_msgid), оригинальные для doc/audio
- Кеш: `./storage/media/.cache.json` по `file_unique_id`, переживает рестарт

## Правила разработки

- Секреты только через .env
- `.env` в .gitignore
- Логи в `./logs/kesha.log` с ротацией (10MB x 5)
- Auto-retry при ошибках сессии (2 попытки)
- Ошибки batch → fallback в ТГ чат

## PROCESS RULES

- Этот проект — бот через который Максим общается с Кешей в Telegram
- CWD бота = `/mnt/data/Рабочий стол/Cursor/COG-second-brain`
- Systemd сервис `kesha-bot`, sudoers для restart без пароля
- После правок: `sudo systemctl restart kesha-bot`
- MCP тулы: `mcp__kesha__*`

## TODO

См. [TODO.md](TODO.md)
