"""Рантайм сдался на разборе своего вызова инструмента — это не ответ.

21.09.2026 Катя получила на голосовое ровно одну строку: «The model's tool call
could not be parsed (retry also failed)». Для бота это выглядело успешным
ответом (`tools=0, no retry needed`), потому что CLI отдаёт свою капитуляцию
обычным текстом. Что ломается без этого кода: пользователь вместо ответа
получает английскую техническую строку, а вопрос теряется.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import response_stream  # noqa: E402
from test_runtime_limits import FakeBot, FakeRegistry, visible_text  # noqa: E402

GIVEUP = (
    "The model's tool call could not be parsed (retry also failed)."
    "Error: The model's tool call could not be parsed (retry also failed)."
)


class GiveupSession:
    """Отдаёт капитуляцию рантайма, а со следующей попытки — настоящий ответ."""

    def __init__(self, *, fail_times=1, answer="Записала, считаю по порядку"):
        self.model = "claude-opus-5"
        self.session_id = "sid"
        self.usage_limit_active = False
        self.calls = 0
        self._fail_times = fail_times
        self._answer = answer

    def quota_summary(self):
        return None

    def reset_response_usage(self):
        pass

    async def check_context_reserve(self, prompt="", **kw):
        return {"ok": True}

    async def send_message(self, prompt):
        self.calls += 1
        if self.calls <= self._fail_times:
            yield {"type": "text", "content": GIVEUP}
            return
        yield {"type": "text", "content": self._answer}


async def run_turn(session):
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(session, "claude"))

    async def noop():
        return None

    typer = asyncio.create_task(noop())
    await typer
    await response_stream._ask_inner(None, "вопрос голосом", 893553748, typer)
    return bot


@pytest.mark.asyncio
async def test_giveup_is_retried_and_never_shown_as_an_answer():
    session = GiveupSession()
    bot = await run_turn(session)
    text = visible_text(bot)

    assert session.calls == 2, "вопрос не был задан заново"
    assert "could not be parsed" not in text, "техническая строка ушла пользователю"
    assert "Записала" in text, "настоящий ответ не доехал"


@pytest.mark.asyncio
async def test_after_all_retries_the_user_is_told_in_his_language():
    """Повторы кончились — объясняем по-русски, а не пересылаем английскую ошибку."""
    session = GiveupSession(fail_times=99)
    bot = await run_turn(session)
    text = visible_text(bot)

    assert "could not be parsed" not in text
    assert "повтори вопрос" in text.lower()
