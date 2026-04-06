# Kesha TG Bot

Telegram-бот на `ClaudeSDKClient` (persistent connection) из официального `claude-agent-sdk`.

## Архитектура

```
Telegram (Aiogram 3) → bot.py → claude_session.py → ClaudeSDKClient → claude CLI → OAuth
```

- **bot.py** — handlers, дебаунс, i18n, нативный стриминг (SendMessageDraft), media cache, album support, tool display, message injection
- **claude_session.py** — `ClaudeSDKClient` persistent connection, interrupt, live model change, context usage
- **kesha_tools.py** — MCP tools: самонастройка, отправка медиа (photo/file/video/audio/voice), schedule_message
- **system_prompt.txt** — system prompt для Claude (TG контекст, форматирование, self-config)
- **setup_wizard.py** — интерактивная настройка .env при первом запуске

## Сессии

- Первое сообщение → `client.connect(prompt)` → новая сессия → `session_id` в `./storage/session_id`
- Следующие → `client.query(prompt)` → persistent connection, контекст сохраняется
- `/clear` → disconnect + сброс → новая сессия
- Session переживает рестарт бота (persistent file)
- При ошибке connection → автореконнект

## Message Injection

- Пока Claude думает → новое сообщение отправляется через `client.query()` напрямую
- Claude получает его как follow-up и учитывает в текущем ответе
- Как в Claude Code CLI — можно печатать пока он работает

## Стриминг

- `include_partial_messages=True` → `StreamEvent` с `text_delta`
- Нативный стриминг через `SendMessageDraft` (Bot API 9.5) — без мерцания от editMessage
- Text → draft стримится в реальном времени → финализируется как реальное сообщение
- Tool → отдельный бабл `🔧 Read /path` → text после tool стримится в тот же бабл (edit)
- Каждый завершённый text block = отдельное сообщение

## Interrupt

- `/stop` → `client.interrupt()` через SDK + мягкий cancel
- Сохраняет уже сгенерированный текст + `_(stopped)_`
- Сессия не ломается

## Дебаунс

- Сообщение → N сек ожидание (по умолчанию 3, `/debounce`) → склейка в батч
- Батч: `--- message 1/N ---` разделители
- `[Имя Фамилия (@username)]:` перед каждым промптом

## Формат медиа в промптах

- `[photo: path]` + caption на отдельной строке
- `[voice: path | расшифровка]`
- `[video_note: path | расшифровка]` (ffmpeg → Deepgram)
- `[document: path (filename)]` + caption
- `[video: path]`, `[audio: path (name)]`, `[sticker: emoji]`
- Форварды: `[Forwarded from Name]`
- Реплаи: `[reply: "цитата"]`
- Альбомы: `aiogram-media-group` группирует фото/видео в один блок

## MCP Tools (kesha)

- `set_model` — сменить модель (live через client.set_model)
- `set_debounce` — задержка склейки
- `toggle_debug` — debug логи
- `get_bot_status` — полный статус (model, session, context usage, rate limit, cost, uptime)
- `restart_bot` — перезапуск через systemd
- `send_photo` — отправить фото в ТГ
- `send_file` — отправить файл в ТГ
- `send_video` — отправить видео (с плеером)
- `send_audio` — отправить аудио (с плеером)
- `send_voice` — отправить голосовое
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
