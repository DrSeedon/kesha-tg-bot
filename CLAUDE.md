# Kesha TG Bot

Telegram-бот на `ClaudeSDKClient` (persistent connection) из официального `claude-agent-sdk`.

## Архитектура (v2.1 — single-node, no failover/Redis)

```
Telegram (Aiogram 3) → handlers.py → chat_state.py (ChatState) → response_stream.py → claude_session.py → Claude CLI
```

### Модули

| Файл | Строк | Что делает |
|------|-------|-----------|
| **bot.py** | ~200 | Bootstrap: bot/dp creation, main(), singleton lock, wiring |
| **config.py** | ~200 | Env, logging, STRINGS, t(), ALLOWED_MODELS |
| **chat_state.py** | ~620 | ChatPhase state machine, PendingEntry, ChatState, ChatRegistry |
| **handlers.py** | ~540 | Все @dp.message handlers, set_commands() |
| **response_stream.py** | ~240 | _ask() — streaming, drafts, ToolStatusTracker, retries |
| **telegram_io.py** | ~170 | user_prefix, _send_safe, split_msg, typing_loop, draft helpers |
| **media.py** | ~200 | download_file, transcribe (aiohttp), caches, cleanup |
| **claude_session.py** | ~300 | ClaudeSDKClient wrapper (file-only session persistence), inject, interrupt, can_use_tool |
| **tool_status.py** | ~225 | Live tool status bubble с таймерами |
| **compact.py** | ~140 | Context compaction (summarize → reset → continue) |
| **kesha_tools.py** | ~400 | MCP tools: send_media, reminders, config, search_memory, run_on_laptop |
| **reminders.py** | ~360 | SQLite reminders (plain/urgent_llm/lazy_llm) |
| **message_log.py** | ~80 | SQLite full message logging (user+assistant), on_message callback for RAG |
| **rag.py** | ~260 | RAG semantic memory: e5-small int8 + sqlite-vec + FTS5 hybrid search + chunking |

### ChatState — центр per-chat state

Каждый чат имеет свой `ChatState` с фазами:
```
IDLE → COLLECTING → PROCESSING → IDLE
                         ↓
                    COMPACTING → IDLE
          /stop → STOPPING → IDLE
```

Вся мутация per-chat state — только через ChatState API (`accept_entry`, `request_stop`, `request_clear`, `request_compact`, `set_debounce`). Никаких глобальных dict/set.

## Сессии

- Per-chat session files: `./storage/sessions/<chat_id>`
- `ChatRegistry.get(chat_id)` → lazy create ClaudeSession + ChatState
- `/clear` → `request_clear()` → reset session (rejected during PROCESSING)
- Session переживает рестарт бота (persistent file)

## Message Flow

1. TG message → `handlers.py` → `PendingEntry` → `ChatState.accept_entry()`
2. Debounce (default 3s) → batch → `_run_batch()` → `_ask()`
3. During PROCESSING: new messages → `session.inject()` or queue to deferred
4. After response: auto-compact check → drain deferred → IDLE

## Стриминг

- `SendMessageDraft` (Bot API 9.5) — нативная анимация печати
- Tool calls → отдельный `ToolStatusTracker` bubble с таймерами
- Markdown V1 escape для tool hints

## MCP Tools (kesha)

- `set_debounce`, `toggle_debug`, `get_bot_status`, `restart_bot`
- `send_photo`, `send_file`, `send_video`, `send_audio`, `send_voice`
- `create_reminder`, `list_reminders`, `cancel_reminder`, `update_reminder`
- `search_memory` — RAG семантический поиск по всей истории диалогов (e5-small int8 + sqlite-vec + FTS5 hybrid)
- `run_on_laptop` — SSH команды на ноуте через reverse tunnel (whitelist)
- Context compaction is automatic (95% threshold) and via /compact command — no MCP tool
- `react` — emoji reactions
- `react` — emoji reactions

## PROCESS RULES

- **Прод = VPS** (single-node, no failover). Деплой: `deploy@72.56.235.40`, код в `/opt/kesha-bot`, CWD бота = `/opt/cog-second-brain`
- Systemd сервис на VPS: `kesha-bot-vps`. Деплой: `ssh deploy@72.56.235.40 "sudo -u kesha git -C /opt/kesha-bot pull && sudo -n systemctl restart kesha-bot-vps"`
- Локальный сервис (ноут): `kesha-bot` (disabled, не автостарт — failover убран)
- Smoke test: `python -c "import bot"` перед рестартом
- MCP тулы в Кеше: `mcp__kesha__*`
- VPS в РФ → нужен прокси для Anthropic API И Telegram API (Xray → Ёжик VPN, `http://127.0.0.1:10809`). `NO_PROXY=localhost,127.0.0.1` — НЕ добавлять api.telegram.org (РКН блокирует, нужен прокси)

## VPS TROUBLESHOOTING (шпаргалка)

**Ребут бота:**
```bash
ssh deploy@72.56.235.40 "sudo -n systemctl restart kesha-bot-vps"
```

**Логи:**
```bash
ssh deploy@72.56.235.40 "sudo -n journalctl -u kesha-bot-vps --no-pager -n 50"
```

**Деплой (git pull + restart):**
```bash
ssh deploy@72.56.235.40 "sudo -u kesha git -C /opt/kesha-bot pull && sudo -n systemctl restart kesha-bot-vps"
```

**401 / "Failed to authenticate" → токен протух:**
```bash
ssh deploy@72.56.235.40
sudo -u kesha -i
HTTPS_PROXY=http://127.0.0.1:10809 claude auth login
# → открыть ссылку в браузере → авторизоваться → вставить код
exit
sudo -n systemctl restart kesha-bot-vps
```

**Claude CLI на VPS (ручной запуск):**
```bash
sudo -u kesha -i
HTTPS_PROXY=http://127.0.0.1:10809 claude
```

**Статус сервиса:**
```bash
ssh deploy@72.56.235.40 "sudo -n systemctl status kesha-bot-vps --no-pager | head -8"
```

## TODO

См. [TODO.md](TODO.md)
