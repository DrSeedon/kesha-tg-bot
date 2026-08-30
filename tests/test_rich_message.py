import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types

import handlers


def _rich_message(blocks):
    return types.Message.model_validate(
        {
            "message_id": 37283,
            "date": 1750000000,
            "chat": {"id": 42, "type": "private"},
            "from": {
                "id": 7,
                "is_bot": False,
                "first_name": "Test",
            },
            "rich_message": {"blocks": blocks},
        }
    )


def _unknown_message(text=None):
    msg = MagicMock()
    msg.chat.id = 42
    msg.from_user.id = 7
    msg.text = text
    msg.caption = None
    msg.content_type = "unknown"
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            _rich_message([{"type": "paragraph", "text": "А это"}]),
            "rich",
        ),
        (_rich_message([]), "rich"),
        (_unknown_message("legacy text"), "unknown-text"),
        (_unknown_message(), "unknown-empty"),
    ],
    ids=["rich-paragraph", "rich-empty-blocks", "unknown-with-text", "unknown-empty"],
)
async def test_t1_fallback_handles_rich_and_unknown_messages(
    monkeypatch, message, expected
):
    enqueue = AsyncMock()
    monkeypatch.setattr(handlers, "allowed", lambda _uid: True)
    monkeypatch.setattr(handlers, "enqueue", enqueue)

    await handlers.h_fallback(message)

    enqueue.assert_awaited_once()
    prompt = enqueue.await_args.args[1]
    if expected == "rich":
        prefix = "[rich_message] "
        assert prompt.startswith(prefix)
        assert json.loads(prompt[len(prefix) :]) == message.rich_message.model_dump(
            exclude_none=True
        )
    elif expected == "unknown-text":
        assert prompt == "legacy text"
    else:
        assert prompt == "[unhandled message: unknown]"
