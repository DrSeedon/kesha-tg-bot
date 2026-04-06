# Kesha TG Bot

Telegram bot powered by **Claude Agent SDK** (official Anthropic SDK). A full Claude Code CLI experience, but through Telegram.

*[Русский](#русский) ниже.*

## What is this

One bot = one `claude` process with a persistent session. Like chatting in Claude Code terminal, but via Telegram. All CLAUDE.md, memory files, MCP servers, tools — picked up from the working directory.

## Features

- **Text** — regular messages → Claude responds
- **Photos** — downloaded, sent to Claude for analysis
- **Voice** — Deepgram STT → text → Claude
- **Documents** — downloads file, Claude can read it
- **Video / Audio** — downloads media
- **Video notes** — downloads mp4 circles
- **Stickers** — passes emoji to Claude
- **Forwards** — tagged with [Forwarded from Name]
- **Persistent session** — resume between messages, context preserved
- **Auto-retry** — on session error, auto-recreates (2 attempts)
- **Typing indicator** — spinning while Claude thinks
- **Debounce** — waits 3 sec to batch multiple messages into one prompt
- **i18n** — Russian and English UI based on Telegram language
- **Debug mode** — toggle with `/debug`, full logging to file
- **Media storage** — local `./storage/media/` with auto-cleanup (24h)

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Bot status & session info |
| `/clear` | Reset session (new context) |
| `/ping` | Check if bot is alive |
| `/model claude-opus-4-6` | Change Claude model |
| `/debug` | Toggle debug logging |

## Quick Start

```bash
git clone <repo-url> && cd kesha-tg-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Interactive setup:
python setup_wizard.py

# Or manually:
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN

python bot.py
```

## Environment Variables (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | **required** |
| `ALLOWED_USERS` | Telegram user IDs, comma-separated | all |
| `CLAUDE_MODEL` | Claude model | claude-sonnet-4-6 |
| `WORK_DIR` | Working directory (with CLAUDE.md) | `.` (current) |
| `DEEPGRAM_API_KEY` | Deepgram key for voice messages | optional |
| `DEBUG` | Enable debug logging | false |
| `MEDIA_DIR` | Media storage path | ./storage/media |
| `LOG_DIR` | Log files path | ./logs |

## Systemd (auto-start)

```bash
sudo cp kesha-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kesha-bot
sudo systemctl start kesha-bot
```

## Architecture

```
Telegram → Aiogram 3 → bot.py → claude_session.py → claude-agent-sdk → claude CLI (OAuth)
                                       ↓
                                 resume=session_id
                                       ↓
                              Persistent conversation
```

- `bot.py` — Aiogram handlers for all media types, debounce, i18n
- `claude_session.py` — wrapper over `claude-agent-sdk` with resume
- `setup_wizard.py` — interactive first-run configuration

## Stack

- Python 3.11+
- aiogram 3.x
- claude-agent-sdk (official Anthropic)
- Deepgram Nova-2 (STT)

---

# Русский

Телеграм-бот на **Claude Agent SDK** (официальный SDK от Anthropic). Полная копия Claude Code CLI, но через Telegram.

## Что это

Один бот = один `claude` процесс с persistent session. Как общаться в терминале Claude Code, только через ТГ. Все CLAUDE.md, memory, MCP серверы, tools — подхватываются из рабочей директории.

## Возможности

- **Текст** — обычные сообщения → Claude отвечает
- **Фото** — скачивает, передаёт Claude для анализа
- **Голосовые** — Deepgram STT → текст → Claude
- **Документы** — скачивает файл, Claude может прочитать
- **Видео / Аудио** — скачивает медиа
- **Видеокружки** — скачивает mp4
- **Стикеры** — передаёт emoji
- **Пересланные** — помечает [Переслано от Имя]
- **Persistent session** — resume между сообщениями, контекст сохраняется
- **Auto-retry** — при ошибке сессии автоматически пересоздаёт (2 попытки)
- **Typing indicator** — крутится пока Claude думает
- **Дебаунс** — ждёт 3 сек для склейки нескольких сообщений в один промпт
- **i18n** — русский и английский интерфейс по языку Telegram
- **Debug режим** — вкл/выкл через `/debug`, полное логирование в файл
- **Хранилище медиа** — локальное `./storage/media/` с автоочисткой (24ч)

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Статус бота и сессии |
| `/clear` | Сбросить сессию (новый контекст) |
| `/ping` | Проверить что бот жив |
| `/model claude-opus-4-6` | Сменить модель |
| `/debug` | Вкл/выкл debug логирование |

## Быстрый старт

```bash
git clone <repo-url> && cd kesha-tg-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Интерактивная настройка:
python setup_wizard.py

# Или вручную:
cp .env.example .env
# Отредактировать .env — вписать TELEGRAM_BOT_TOKEN

python bot.py
```

## Переменные окружения (.env)

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `TELEGRAM_BOT_TOKEN` | Токен бота из @BotFather | **обязательно** |
| `ALLOWED_USERS` | Telegram user IDs через запятую | все |
| `CLAUDE_MODEL` | Модель Claude | claude-sonnet-4-6 |
| `WORK_DIR` | Рабочая директория (с CLAUDE.md) | `.` (текущая) |
| `DEEPGRAM_API_KEY` | Ключ Deepgram для голосовых | опционально |
| `DEBUG` | Включить debug логирование | false |
| `MEDIA_DIR` | Путь для хранения медиа | ./storage/media |
| `LOG_DIR` | Путь для логов | ./logs |
