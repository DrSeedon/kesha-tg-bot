# Task #32 research: forwarded rich messages

Date: 2026-08-30

## Question

- **Context:** Aiogram 3 message ingress dispatches ordinary text/media to dedicated handlers and all remaining messages to handlers.h_fallback; ChatState.accept_entry() is the existing durable admission path.
- **Change under test:** represent a parsed Message.rich_message as a non-empty string at the fallback boundary so the existing enqueue() path receives it.
- **Baseline:** h_fallback currently reads only msg.text and msg.caption.
- **Measurable outcome:** a valid ContentType.RICH_MESSAGE update with no text/caption causes exactly one enqueue() call with non-empty content; ordinary text/media/forward/debounce tests remain unchanged.

## Hypotheses considered

1. **H1 — rich_message is the field that sets the content type.** The installed Message.content_type checks the parsed rich_message object and returns ContentType.RICH_MESSAGE. Falsifier: a parsed message with only rich_message returns another content type or the field is not present in the installed model.
2. **H2 — the current loss is the fallback's text-only gate.** A valid rich update has text is None and caption is None, so the fallback logs and does not enqueue. Falsifier: the direct fallback probe invokes enqueue() or the parsed update contains text/caption.
3. **H3 — JSON serialization of the parsed object is the smallest lossless boundary representation available now.** RichMessage.model_dump_json(exclude_none=True) retains every non-null field that aiogram parsed, including nested block/media fields, while producing a plain string accepted by enqueue(). Falsifier: serialization errors or omitted non-null parsed fields.
4. **H4 — extracting only visible text is sufficient.** Falsifier: a supported rich block contains attachment/structural fields that a text-only extractor omits. The installed schema includes media blocks and non-text blocks, so this hypothesis is rejected.

## Findings

### Required fact table

The table deliberately records observations only. Conclusions and implementation choices are below it.

| evidence source/version | exact message/content field | value/shape and nullability | current handler path | currently preserved data | currently lost data | safe user-visible serialization candidate | exact test-fixture provenance/limitations |
|---|---|---|---|---|---|---|---|
| Installed /home/kesha/projects/kesha-tg-bot/.venv, aiogram 3.30.0; aiogram/types/message.py:388; aiogram 3.30 docs [6] | Message.rich_message | RichMessage or None, default None; optional field. RichMessage has required blocks: list[RichBlockUnion] and optional is_rtl: bool or None (rich_message.py:19-22). | Message.content_type reaches the rich_message branch and returns ContentType.RICH_MESSAGE (message.py:703-849); enum value is rich_message (enums/content_type.py:85). | Parsed Message fields remain available to the fallback call. | text and caption remain None; they are not populated from rich_message. | Measured plain string: msg.rich_message.model_dump_json(exclude_none=True), optionally prefixed with [rich_message]. | Synthetic Message.model_validate() envelope with message_id, date, chat, from, and rich_message; no raw production JSON was available. |
| Installed aiogram 3.30.0, types/rich_block_union.py:27-50; official API [5] | RichMessage.blocks | Discriminated list (Field discriminator="type") of exactly 21 installed block classes: paragraph, heading, pre, footer, divider, mathematical_expression, anchor, list, blockquote, pullquote, collage, slideshow, table, details, map, animation, audio, photo, video, voice_note, thinking. List itself may be empty (measured). | No dedicated dispatcher filter/handler is registered for rich blocks; F.text does not match when Message.text is None, so dispatch falls through to h_fallback (handlers.py:752-763). | blocks, each recognized block's fields, nested lists/captions and parsed media objects are in msg.rich_message. | No block data reaches PendingEntry, ChatState, or messages.db under the current empty-text gate. | JSON output retains the block type and all non-null block fields. Measured output for a paragraph/photo/list/table fixture retained all supplied fields. | The block list is from installed source, not a Telegram capture. Current Bot API also lists newer document, buttons, and expandable-quote blocks [5]; those are outside this installed union. |
| Installed aiogram 3.30.0, types/rich_text_union.py:64-96; official API [5] | Text inside block fields (paragraph.text, heading.text, captions, etc.) | RichTextUnion accepts str, recursively nested list, or typed rich-text objects (bold/italic/underline/strikethrough/spoiler/date_time/text_mention/subscript/superscript/marked/code/custom_emoji/mathematical_expression/url/email_address/phone_number/bank_card_number/mention/hashtag/cashtag/bot_command/anchor/anchor_link/reference/reference_link). Text-bearing fields are required where the block declares them; media captions/credits are optional. | h_fallback reads only top-level msg.text/msg.caption, never nested rich-text fields (handlers.py:656-663). | The parsed nested rich-text object is present on msg.rich_message. | Nested visible text, links, formatting types, captions, credits, and metadata are all absent from the prompt/log when fallback does not enqueue. | JSON preserves both plain strings and typed rich-text nodes; a text-only flattening would not preserve link targets, formatting or block boundaries. | Fixture used a plain paragraph and a nested bold heading; it does not claim coverage of every recursive rich-text variant. |
| Installed runtime probe, aiogram 3.30.0, current repository handlers.py:656-663 | h_fallback source values | For a valid parsed rich message: content_type=ContentType.RICH_MESSAGE, msg.text is None, msg.caption is None; warning preview is empty. | text = msg.text or msg.caption or ""; warning is logged; if text is false; enqueue is not called. | Warning line preserves the content type and an empty preview. | The whole rich payload is discarded; no PendingEntry is created and no later ChatState/messages.db write can occur. | Probe passed the measured JSON string to enqueue, which produced one PendingEntry with message_id=37283 and reply_target=42. | Direct probe monkey-patched handlers.enqueue with AsyncMock; it verifies the current branch but does not run a real Telegram update through a Dispatcher. |
| Installed runtime parse probe, aiogram 3.30.0; current tests/test_activity_ingress.py | Minimal incoming update envelope | Message.model_validate() accepted rich_message={"blocks":[{"type":"paragraph","text":"А это"}]} and returned ContentType.RICH_MESSAGE; blocks=[] also parsed and still returned RICH_MESSAGE; rich_message=None returned UNKNOWN. | Valid parsed object reaches fallback; malformed payload fails before handler dispatch. | Recognized fields plus extra fields on recognized Telegram objects are retained because TelegramObject.model_config.extra == allow. | Unknown block discriminator values are not accepted by the installed union; a synthetic type=document block raised ValidationError. | model_dump_json(exclude_none=True) emitted {"blocks":[{"type":"paragraph","text":"А это"}]} for the minimal fixture. | No tests/test_rich_message.py exists in this checkout. Existing activity ingress tests are MagicMock-based and do not model rich_message; they passed 15 passed and provide regression coverage only for existing admission/media paths. |

## Conclusions for the later plan

- H1 and H2 are **CONFIRMED — direct installed-source inspection plus runtime probes**. The exact trigger is a non-None Message.rich_message; the exact loss is h_fallback's empty text/caption gate.
- H3 is **CONFIRMED for the installed schema — runtime serialization probe plus aiogram's generated model fields**. The smallest safe boundary is a non-empty plain string containing model_dump_json(exclude_none=True); optional None fields are semantically absent, while False, 0, empty lists, and all non-null fields remain in the output.
- H4 is **REFUTED — installed schema inspection**. Rich messages can contain media blocks (photo, video, audio, animation, voice_note) and structural blocks (table, list, details, etc.), so text-only flattening would discard fields that Telegram delivered.
- The narrow implementation surface is handlers.py:h_fallback plus a new focused tests/test_rich_message.py. A helper in telegram_io.py is optional; no ChatState, message-log, media-download, dispatcher-order, or debounce change is indicated by the evidence.
- The focused acceptance command named by the user cannot run yet because tests/test_rich_message.py is absent in this phase. The existing half of it ran as /home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest -q tests/test_activity_ingress.py -> 15 passed in 8.89s.

## Counter-evidence and rejected hypotheses

- **Debounce/media-group failure:** rejected by the supplied production timeline: a normal forwarded text (msg_id=37278) entered the pipeline; the later RICH_MESSAGE warning had no enqueue. Current registration also puts h_text before the catch-all and has no rich-message-specific media-group path (handlers.py:752-763).
- **Telegram did not deliver the attachment:** rejected by the supplied warning containing ContentType.RICH_MESSAGE; the installed parser probe independently confirms that the field is a valid parsed Message field. No production payload was read in this research.
- **Rich messages are always text-compatible:** rejected by the installed block union and media fixture. A plain-text-only extraction would lose media identifiers/dimensions, captions, credits, formatting nodes, and structural block fields.
- **The installed package parses every current Bot API rich block:** rejected. Official current Bot API documentation lists newer block types (expandable_blockquote, buttons, document) that are not in aiogram 3.30.0's RichBlockUnion [5][6]. The fix must not invent or hand-author fixtures for those types.
- **A parser bypass is needed:** not supported by evidence. An unknown block discriminator raises ValidationError before this handler; handling that would be a separate dependency/schema compatibility task.

## Affected files, risks, and edge cases

- handlers.py:h_fallback: currently the only loss point. Preserve the warning and call enqueue once with a non-empty representation.
- tests/test_rich_message.py: add a real aiogram Message.model_validate() fixture using only installed 3.30.0 classes; assert content type, non-empty serialization, and exactly one enqueue/admission. Keep raw payload values synthetic and explicit.
- Existing ordinary handlers and ChatState.accept_entry() should remain untouched. enqueue already supplies user_prefix, forward/reply metadata, PendingEntry.message_id, chat routing, debounce, and eventual message-log admission.
- Empty blocks is parseable and still needs a non-empty envelope so it is not silently dropped.
- is_rtl and optional nested fields may be absent or null; exclude_none=True intentionally omits only those semantically absent values.
- Recognized blocks may carry extra fields (extra=allow); JSON serialization keeps them. Unknown block types cannot reach this handler with aiogram 3.30.0 and remain out of scope.
- Nested media are descriptions only at this boundary. Current media download functions expect top-level Message.photo/video/etc.; downloading rich-block media would be a separate feature and is not required to prevent this update from disappearing.

## Review / completeness check

Review route: mechanical completeness for fact extraction. Codex is unavailable in the current quota window, so no model review was run (Review: none — Codex unavailable). Mechanical checks completed: every key schema claim has an installed-source line or runtime output; both positive and negative parser cases were run; the required fact table contains all eight requested columns; rejected hypotheses and fixture limitations are explicit.

## Sources

1. Installed package metadata: /home/kesha/projects/kesha-tg-bot/.venv/lib/python3.12/site-packages/aiogram-3.30.0.dist-info/METADATA (Version: 3.30.0, evidence tier: primary local artifact).
2. Installed aiogram source: aiogram/types/message.py, enums/content_type.py, types/base.py (evidence tier: primary source; lines cited inline).
3. Installed aiogram source: aiogram/types/rich_message.py, rich_block_union.py, rich_text_union.py, and block/text model files (evidence tier: primary source; lines cited inline).
4. Runtime probes executed with /home/kesha/projects/kesha-tg-bot/.venv/bin/python on 2026-08-30 (evidence tier: direct measurement): Message.model_validate, RichMessage.model_dump_json, direct h_fallback, and enqueue with AsyncMock registry.
5. [Telegram Bot API — RichMessage, RichText, and RichBlock](https://core.telegram.org/bots/api#richmessage) (opened 2026-08-30; evidence tier: primary external source; current Bot API 10.3, including newer block types).
6. [aiogram 3.30.0 RichMessage documentation](https://docs.aiogram.dev/en/v3.30.0/api/types/rich_message.html) (opened 2026-08-30; evidence tier: primary generated documentation for the installed release).
