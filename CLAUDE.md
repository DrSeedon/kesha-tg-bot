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
| **response_stream.py** | ~270 | _ask() — streaming via send+edit_message_text, ToolStatusTracker, retries |
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

- **Прод = Contabo VPS** (158.220.127.161, Франция, 8GB, single-node). Доступ: `ssh root@158.220.127.161`. Код в `/opt/kesha-bot`, CWD бота = `/opt/cog-second-brain`, бот-юзер `kesha` (uid 1001)
- Systemd сервис: `kesha-bot-vps`. Деплой: `ssh root@158.220.127.161 "sudo -u kesha git -C /opt/kesha-bot pull && systemctl restart kesha-bot-vps"`
- **БЕЗ ПРОКСИ**: Contabo во Франции (не РФ) → достаёт api.anthropic.com / api.telegram.org / api.deepgram.com напрямую. НЕТ `HTTPS_PROXY`/`TG_PROXY`/`NO_PROXY` в .env и unit. Не добавлять — Ёжик/Xray для исходящего не нужен
- ⚠️ На Contabo уже крутится Xray Максима (порты 443/8443/4443 inbound VLESS) — НЕ трогать, к боту отношения не имеет
- Локальный сервис (ноут): `kesha-bot` (disabled, не автостарт — failover убран)
- Smoke test: `python -c "import bot"` перед рестартом
- MCP тулы в Кеше: `mcp__kesha__*`
- **Старый прод (Timeweb 72.56.235.40, Москва)** — выведен из эксплуатации при миграции (#6, 2026-07-03). Данные оставлены как rollback-бэкап, сервис `kesha-bot-vps` там stop+disable. Не деплоить туда

## VPS TROUBLESHOOTING (шпаргалка) — Contabo 158.220.127.161

**Ребут бота:**
```bash
ssh root@158.220.127.161 "systemctl restart kesha-bot-vps"
```

**Логи:**
```bash
ssh root@158.220.127.161 "journalctl -u kesha-bot-vps --no-pager -n 50"
```

**Деплой (git pull + restart):**
```bash
ssh root@158.220.127.161 "sudo -u kesha git -C /opt/kesha-bot pull && systemctl restart kesha-bot-vps"
```

**401 / "Failed to authenticate" → токен протух (БЕЗ прокси, Contabo достаёт напрямую):**
```bash
ssh root@158.220.127.161
sudo -u kesha -i
claude auth login
# → открыть ссылку в браузере → авторизоваться → вставить код
exit
systemctl restart kesha-bot-vps
```

**Claude CLI на VPS (ручной запуск):**
```bash
sudo -u kesha -i
claude
```

**Статус сервиса:**
```bash
ssh root@158.220.127.161 "systemctl status kesha-bot-vps --no-pager | head -8"
```

## Session notes (2026-06-27)

### RAG Memory — полная хронология
- v2.3.0: MiniLM + sqlite-vec + FTS5 hybrid → качество 2.2/5
- v2.3.1: e5-large int8 (561MB) → OOM на VPS 2.9GB → mpnet тоже OOM → откат на MiniLM
- v2.3.2: e5-small int8 (Xenova/multilingual-e5-small, 118MB, ONNX) + batch_size=16 + arena-off → качество 4.3/5, RAM стабильный
- **Root cause OOM**: FastEmbed грузил все docs одним вызовом → onnxruntime arena раздувалась. Fix: batch_size=16 + enable_cpu_mem_arena=False
- **VPS RAM budget**: 2.9GB total, Кеша ~966MB (бот+CLI+5 MCP+embedder), 1.4GB available, swap 0
- Кеша сам отключал RAG на VPS (закомментировал import rag в bot.py) когда OOM убил VPN — потом восстановили через `git checkout -- bot.py`

### Reverse SSH Tunnel
- Ноут → VPS (tunnel@158.220.127.161, Contabo) → порт 2222 на localhost. (До миграции #6 был tunnel@72.56.235.40)
- Ключи: `~/.ssh/tunnel_vps` (ноут→VPS), `/home/kesha/.ssh/tunnel_laptop` (VPS→ноут)
- systemd unit: `ssh-tunnel-vps.service` на ноуте (enabled, Restart=always)
- `run_on_laptop` MCP tool с whitelist команд (kill, pkill, sudo reboot, sudo systemctl restart orchestra)
- Безопасность: ключи НЕ в git, tunnel юзер restricted (no shell), порт 2222 только localhost

### Proxy / VPN на VPS
- **После миграции #6 (Contabo, Франция) — ПРОКСИ НЕ НУЖЕН.** Contabo достаёт api.anthropic.com / api.telegram.org / api.deepgram.com напрямую (проверено: HTTP 401/302/404, ~14ms). Прокси-обвязка выпилена из .env и systemd unit
- `bot.py:43` `_tg_proxy = os.getenv("TG_PROXY") or os.getenv("HTTPS_PROXY") or None` → при unset = None = direct (код это переживает нативно)
- **(Историческое, Timeweb в РФ)**: там был Xray → Ёжик VPN (`http://127.0.0.1:10809`), `TG_PROXY`→aiogram, `HTTPS_PROXY`→Claude SDK, `NO_PROXY=localhost,127.0.0.1`. Не воспроизводить на Contabo
- ТСПУ (РКН) периодически блокирует трафик к VPS — это не наша проблема

### Workers alive
- `rag-research` (opus 4.8, ctx:33%) — RAG research/benchmark, idle
- `kesha-p0-fix` (opus 4.6, ctx:15%) — P0/P1 bugfixes + reverse tunnel + message_log, idle
- `code-review` (opus 4.6, ctx:6%) — old code review, idle

## TODO

См. [TODO.md](TODO.md)
