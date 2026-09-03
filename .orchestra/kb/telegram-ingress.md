# telegram-ingress

## Установлено

- Installed aiogram 3.30.0 models Message.rich_message as optional RichMessage; a non-None object makes Message.content_type return ContentType.RICH_MESSAGE · /home/kesha/projects/kesha-tg-bot/.venv/lib/python3.12/site-packages/aiogram/types/message.py:388,703-849; enums/content_type.py:85 · 2026-08-30, #32
- Installed aiogram 3.30.0 RichMessage requires blocks (a discriminator-based list of 21 installed RichBlock classes) and has optional is_rtl; RichTextUnion allows strings, recursive lists, and typed rich-text nodes · types/rich_message.py:19-22, types/rich_block_union.py:27-50, types/rich_text_union.py:64-96 · 2026-08-30, #32
- handlers.h_fallback currently gates admission on msg.text or msg.caption; a parsed rich message with both absent logs ContentType.RICH_MESSAGE and calls no enqueue · handlers.py:656-663; direct probe enqueue_calls=0 · 2026-08-30, #32
- RichMessage.model_dump_json(exclude_none=True) produced a non-empty plain string retaining supplied paragraph, nested formatting, media, list, and table fields; passing that string to enqueue produced one PendingEntry with the original message id and chat target · runtime probes · 2026-08-30, #32
- TelegramObject uses extra=allow, but an unknown rich-block discriminator (for example type=document) fails Message.model_validate() in aiogram 3.30.0 · types/base.py; runtime negative parse probe · 2026-08-30, #32

## Отвергнуто

- Rich-message loss is caused by debounce or media-group ordering · production timeline plus handlers.py:752-763 and direct fallback probe isolate the empty text/caption gate · 2026-08-30, #32
- Rich messages can be handled as text alone · installed media/structural block union and serialization probe show non-text fields would be omitted · 2026-08-30, #32
- aiogram 3.30.0 can parse every current Bot API rich block · current Telegram API lists newer document/buttons/expandable-quote blocks absent from the installed union · 2026-08-30, #32

## Пробелы

- The exact raw production JSON for msg_id 37283 was not captured; synthetic fixtures prove installed parsing and ingress behavior but not every Telegram client/media combination · production payload was not read in this research · 2026-08-30, #32
- No benchmark compares a human-readable rich-block flattening with JSON; the fix only needs a faithful non-empty handoff for the existing pipeline · separate product-quality evaluation remains open · 2026-08-30, #32

## Источники

- docs/tasks/32/research.md — installed schema, runtime probes, handler path, and fixture limitations.
