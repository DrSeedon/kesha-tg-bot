# TODO

- [x] ~~Redis/SQLite~~ Persistent session_id через файл `./storage/session_id`
- [ ] Стриминг ответов в ТГ (сейчас ждёт полный ответ, потом шлёт)
- [ ] Inline кнопки для частых действий
- [ ] Webhook вместо polling (для production)
- [ ] Rate limiting
- [ ] Per-user сессии (dict[user_id → ClaudeSession])
- [x] Video note транскрипция (ffmpeg → Deepgram)
