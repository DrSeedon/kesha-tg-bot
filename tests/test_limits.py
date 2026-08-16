"""`/limits`: Orchestra is the source of truth, Kesha owns Telegram delivery."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


NOW = dt.datetime(2026, 8, 14, 8, 0, 0, tzinfo=dt.timezone.utc)


def _iso(minutes_ahead: float) -> str:
    return (NOW + dt.timedelta(minutes=minutes_ahead)).isoformat()


def usage() -> dict:
    return {
        "anthropic": {
            "five_hour": {"utilization": 4.0, "resets_at": _iso(270)},
            "seven_day": {"utilization": 88.0, "resets_at": _iso(5700)},
        },
        "codex": {
            "primary": {
                "utilization": 97,
                "window_minutes": 10080,
                "resets_at": _iso(8300),
            },
            "spark": {
                "primary": {
                    "utilization": 9,
                    "window_minutes": 10080,
                    "resets_at": _iso(8400),
                }
            },
        },
        "grok": {},
        "quota_headroom": {
            "rate": 0.1319,
            "available_pct": 91.0,
            "locked_pct": 5.0,
            "windows_left": 0.91,
            "window_hours": 72,
        },
    }


def test_chat_caption_matches_the_card_window_math():
    from limits import format_limits_message

    text = format_limits_message(usage(), now=NOW)

    assert text.splitlines()[0] == "*Лимиты*"
    assert "Claude 5h — осталось 96%; израсходовано 4%; окно (10%)" in text
    assert "Claude 7d — осталось 12%; израсходовано 88%; окно (43%)" in text
    assert "Codex — осталось 3%; израсходовано 97%; окно (18%)" in text
    assert "Spark — осталось 91%; израсходовано 9%; окно (17%)" in text
    assert "Grok — нет данных" in text


def test_orchestra_token_prefers_explicit_env_and_can_read_shared_env(monkeypatch, tmp_path):
    import limits

    shared = tmp_path / "orchestra.env"
    shared.write_text("INTERNAL_TOKEN=shared-token\n", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRA_ENV_FILE", str(shared))
    monkeypatch.delenv("ORCHESTRA_INTERNAL_TOKEN", raising=False)
    assert limits.orchestra_token() == "shared-token"

    monkeypatch.setenv("ORCHESTRA_INTERNAL_TOKEN", "explicit-token")
    assert limits.orchestra_token() == "explicit-token"


@pytest.mark.asyncio
async def test_usage_and_card_use_the_authenticated_local_orchestra_api(monkeypatch):
    import limits

    requests = []

    class Response:
        def __init__(self, path):
            self.path = path
            self.status = 200

        async def json(self):
            return usage()

        async def read(self):
            return b"canonical-png"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, *, timeout):
            self.timeout = timeout

        def get(self, url, *, headers):
            requests.append((url, headers))
            return Response(url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setenv("ORCHESTRA_INTERNAL_TOKEN", "secret")
    monkeypatch.setenv("ORCHESTRA_URL", "http://127.0.0.1:8888/")
    monkeypatch.setattr(limits.aiohttp, "ClientSession", Session)

    assert await limits.fetch_limits_usage() == usage()
    assert await limits.fetch_limits_card() == b"canonical-png"
    assert requests == [
        ("http://127.0.0.1:8888/api/usage", {"Authorization": "Bearer secret"}),
        ("http://127.0.0.1:8888/api/usage/card", {"Authorization": "Bearer secret"}),
    ]


@pytest.mark.asyncio
async def test_limits_handler_sends_the_png_with_the_full_caption(monkeypatch):
    import handlers

    msg = SimpleNamespace(
        text="/limits",
        from_user=SimpleNamespace(id=1, language_code="ru"),
        chat=SimpleNamespace(id=42, type="private"),
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    monkeypatch.setattr(handlers, "fetch_limits_usage", AsyncMock(return_value=usage()))
    monkeypatch.setattr(handlers, "fetch_limits_card", AsyncMock(return_value=b"png"))

    await handlers.h_limits(msg)

    msg.answer_photo.assert_awaited_once()
    call = msg.answer_photo.await_args
    assert "Claude 5h" in call.kwargs["caption"]
    assert "израсходовано" in call.kwargs["caption"]
    assert call.kwargs["photo"].filename == "limits.png"


@pytest.mark.asyncio
async def test_limits_handler_reports_fetch_or_render_failure(monkeypatch):
    import handlers

    msg = SimpleNamespace(
        text="/limits",
        from_user=SimpleNamespace(id=1, language_code="ru"),
        chat=SimpleNamespace(id=42, type="private"),
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    monkeypatch.setattr(
        handlers,
        "fetch_limits_usage",
        AsyncMock(side_effect=TimeoutError()),
    )

    await handlers.h_limits(msg)

    msg.answer_photo.assert_not_awaited()
    assert msg.answer.await_args.args[0] == "❌ /limits: TimeoutError: (без сообщения)"


@pytest.mark.asyncio
async def test_limits_handler_keeps_text_when_the_card_fails(monkeypatch):
    import handlers

    msg = SimpleNamespace(
        text="/limits",
        from_user=SimpleNamespace(id=1, language_code="ru"),
        chat=SimpleNamespace(id=42, type="private"),
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    monkeypatch.setattr(handlers, "fetch_limits_usage", AsyncMock(return_value=usage()))
    monkeypatch.setattr(
        handlers,
        "fetch_limits_card",
        AsyncMock(side_effect=TimeoutError()),
    )

    await handlers.h_limits(msg)

    fallback = msg.answer.await_args.args[0]
    assert fallback.startswith("❌ /limits: TimeoutError: (без сообщения)\n*Лимиты*")
    assert "Claude 5h" in fallback


def test_limits_command_is_published():
    import handlers

    assert "limits" in {command.command for command in handlers.COMMANDS_RU}
    assert "limits" in {command.command for command in handlers.COMMANDS_EN}
