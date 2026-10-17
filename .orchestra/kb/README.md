# Knowledge base

- [telegram-ingress](telegram-ingress.md) — Aiogram message content types, fallback ingress, and rich-message serialization.
- [context-compaction](context-compaction.md) — context admission, Claude/Codex compaction ownership, same-batch continuation, and reserve history.
- [cost-accounting](cost-accounting.md) — стоимость и токены ответа: накопительные `total_cost_usd` и `model_usage`, снимок `AssistantMessage.usage`, восстановленные цены opus-5, разбивка TTL кеша.
- [rules-codex-runtime](rules-codex-runtime.md) — Codex-рантайм Кеши: решения, ToS-основание, дыры моста тулов (chat_id-обход, TOCTOU, хардлинк), как читать блок safety.
- [rules-incident-lessons](rules-incident-lessons.md) — прод-инциденты и замеры: латч лимита с пустым model_usage, порядок подстрок ошибок, TTL кеша 60 мин, Agent SDK на подписке и баны.
- [rules-verification](rules-verification.md) — методика проверок: мутации, окружение сьюта на VPS, грабли веток и message_log, смоук import bot.
