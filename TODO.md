# TODO

## Done
- [x] ~~Redis/SQLite~~ Persistent session_id через файл `./storage/session_id`
- [x] Video note транскрипция (ffmpeg → Deepgram)
- [x] Стриминг ответов в ТГ (edit message по мере генерации, 1.5s интервал)
- [x] Очередь сообщений (дебаунс + merge при занятом Claude)
- [x] Форварды с мета-данными
- [x] Логирование в файл с ротацией
- [x] i18n (RU/EN)
- [x] Media кеширование (file_unique_id → persistent cache)
- [x] Album support (aiogram-media-group)
- [x] MCP tools: send_photo, send_file, schedule_message, set_model, set_debounce, toggle_debug, get_bot_status, restart_bot
- [x] Reply context в промптах
- [x] Native streaming via `sendMessageDraft` (Bot API 9.5) — no more editMessage flickering
- [x] Smart tool/text flow: text blocks stay, tools ephemeral, text after tool replaces tool message

## Open
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling (для production)
- [ ] Rate limiting
- [ ] Per-user сессии (dict[user_id → ClaudeSession])
- [x] MCP image generation (OpenRouter) — generate_image тул в websearch MCP
- [x] Отправка видео/аудио/голосовых из тулзов (send_video, send_audio, send_voice)
