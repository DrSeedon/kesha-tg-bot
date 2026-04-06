# Kesha TG Bot

Telegram-бот — обёртка над Claude Code CLI через официальный `claude-agent-sdk`.

## Архитектура

```
Telegram (Aiogram 3) → bot.py → claude_session.py → claude-agent-sdk → claude CLI → OAuth Max
```

- **bot.py** — handlers для всех типов медиа, дебаунс, i18n (RU/EN), логирование, меню команд, system prompt, self-config
- **claude_session.py** — обёртка над `claude-agent-sdk.query()` с `resume=session_id` для persistent sessions
- **setup_wizard.py** — интерактивная настройка .env при первом запуске

## Как работают сессии

- Первое сообщение → `query()` без resume → создаётся новая сессия → `ResultMessage.session_id` сохраняется
- Следующие сообщения → `query()` с `resume=session_id` → продолжает ту же сессию
- `/clear` → `session_id = None` → следующее сообщение начнёт новую
- Каждый `query()` = отдельный subprocess `claude`, но session на диске общая

## Формат медиа в промптах

- `[photo: /path/to/file | caption text]`
- `[voice: /path/to/file | transcript: расшифровка]`
- `[document: /path/to/file (name.pdf, 2048 bytes) | caption]`
- `[video: /path/to/file | caption]`
- `[audio: /path/to/file (name.mp3)]`
- `[video_note: /path/to/file]`
- `[sticker: emoji]`
- Форварды: `[Forwarded from Name] текст`

## Дебаунс и очередь

- Сообщение приходит → N сек ожидание (по умолчанию 3, настраивается /debounce) → если за это время ещё — склеиваются в один промпт
- Если Claude уже обрабатывает запрос — новые сообщения ставятся в очередь
- Каждому промпту автоматически добавляется `[Имя Фамилия (@username)]:`

## System Prompt

Claude получает system prompt с информацией о среде: что он в ТГ, какие форматы, какие медиа, доступные настройки. Формат ответов — Telegram Markdown.

## CWD и контекст

Бот работает в `WORK_DIR` (из .env, по умолчанию `.`). Claude подхватывает:
- CLAUDE.md (глобальный + проектный)
- Memory файлы из `.claude/memory/`
- MCP серверы (YouGile, websearch, etc.)
- Все tools (Bash, Read, Write, Edit, Grep, Glob, Agent)

## Правила разработки

- Никаких секретов в коде — всё через .env
- `.env` в .gitignore
- Медиа в `./storage/media/` с автоочисткой (24ч)
- Логи в `./logs/kesha.log` с ротацией (10MB x 5)
- Auto-retry при ошибках сессии (2 попытки)
- Typing indicator крутится пока Claude думает

## TODO

См. [TODO.md](TODO.md)
