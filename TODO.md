# TODO

## Done
- [x] Persistent session_id через файл `./storage/session_id`
- [x] Video note транскрипция (ffmpeg → Deepgram)
- [x] Стриминг ответов — нативный SendMessageDraft (Bot API 9.5), без мерцания
- [x] Очередь сообщений (дебаунс + merge при занятом Claude)
- [x] Форварды с мета-данными
- [x] Логирование в файл с ротацией
- [x] i18n (RU/EN)
- [x] Media кеширование (file_unique_id → persistent cache)
- [x] Album support (aiogram-media-group)
- [x] MCP tools: send_photo, send_file, send_video, send_audio, send_voice, schedule_message, set_model, set_debounce, toggle_debug, get_bot_status, restart_bot
- [x] Reply context в промптах
- [x] Smart tool/text flow: tool в отдельном бабле, text после tool в том же бабле
- [x] MCP image generation (OpenRouter) — generate_image тул в websearch MCP
- [x] ClaudeSDKClient — persistent connection вместо query()
- [x] Message injection — вклинивание сообщений пока Claude думает
- [x] Native interrupt через client.interrupt()
- [x] Live model change через client.set_model()
- [x] Context usage через client.get_context_usage()
- [x] /stop — мягкий interrupt с сохранением текста
- [x] Deepgram cost logging

## Open
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling (для production)
- [ ] Rate limiting
- [ ] Per-user сессии (dict[user_id → ClaudeSession])
