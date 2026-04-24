# TODO

## Done
- [x] Persistent session_id через файл `./storage/sessions/<chat_id>`
- [x] Video note транскрипция (ffmpeg → Deepgram)
- [x] Стриминг — нативный SendMessageDraft (Bot API 9.5)
- [x] Дебаунс + merge при занятом Claude
- [x] Форварды с мета-данными
- [x] Логирование с ротацией (daily, 7 дней, Krsk timezone)
- [x] i18n (RU/EN)
- [x] Media кеширование (file_unique_id → persistent cache)
- [x] Album support (aiogram-media-group)
- [x] MCP tools: send_photo/file/video/audio/voice, set_model, set_debounce, toggle_debug, get_bot_status, restart_bot
- [x] Reply context в промптах
- [x] Tool/text flow: tool в отдельном бабле, text после tool в том же бабле
- [x] ClaudeSDKClient — persistent connection
- [x] Message injection — вклинивание во время обработки
- [x] Native interrupt через client.interrupt()
- [x] Live model change, context usage
- [x] /stop — мягкий interrupt
- [x] Reminders — SQLite, 3 типа (plain/urgent_llm/lazy_llm), repeat, lazy TTL promotion
- [x] Stream stall detection (120s timeout, reconnect)
- [x] Context compaction (/compact, auto at 95%, MCP tool)
- [x] Live tool status bubble с таймерами и per-MCP иконками
- [x] Singleton lock (flock) — защита от двух инстансов
- [x] can_use_tool auto-approve callback (обход .claude/skills/ bug)
- [x] **Refactor v2.0** — ChatState state machine, bot.py split (1385→196 строк, 7 модулей)

## Open
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling
- [ ] Rate limiting per-user
