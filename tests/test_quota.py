"""Quota windows (#6): the numbers behind "limit reached".

Formatting is pure — `now` is passed in — so these assert exact strings
against the shape the Orchestra dashboard shows. No network: the HTTP tests
inject a fake aiohttp module.
"""

import asyncio
import json
import sys
import types as pytypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quota  # noqa: E402

# Captured before conftest's autouse fixture swaps it out: the fetch tests here
# exercise the real function, everything else must stay offline.
_REAL_FETCH = quota.fetch_claude_usage

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _claude_payload(five_util, five_in, seven_util, seven_in):
    """`resets_at` as ISO, exactly like the live oauth/usage response."""
    return {
        "five_hour": {"utilization": five_util,
                      "resets_at": (NOW + five_in).isoformat()},
        "seven_day": {"utilization": seven_util,
                      "resets_at": (NOW + seven_in).isoformat()},
    }


# ---------- rendering ----------

def test_the_dashboard_line_is_reproduced_exactly():
    """The user asked for the dashboard's format; this is that format."""
    payload = _claude_payload(99, timedelta(minutes=18, seconds=30),
                              15, timedelta(days=6, hours=19, minutes=8))
    text = quota.render_windows(quota.claude_windows(payload), NOW)

    assert text == ("5h: 99% (94%) 0h 18m · темп +16m\n"
                    "7d: 15% (3%) 6d 19h 8m · темп +20h 20m")


def test_burn_within_the_window_pace_says_ok():
    """Half the window gone at 20% used is under budget — no cooldown owed."""
    payload = _claude_payload(20, timedelta(minutes=150), 3, timedelta(days=3, hours=12))
    text = quota.render_windows(quota.claude_windows(payload), NOW)

    assert text == ("5h: 20% (50%) 2h 30m · темп ok\n"
                    "7d: 3% (50%) 3d 12h 0m · темп ok")


def test_a_window_without_a_reset_time_shows_only_what_is_known():
    """No date must never become an invented date."""
    payload = {"five_hour": {"utilization": 3.0, "resets_at": None},
               "seven_day": {"utilization": 16.0, "resets_at": None}}
    text = quota.render_windows(quota.claude_windows(payload), NOW)

    assert text == "5h: 3%\n7d: 16%"


def test_an_expired_window_drops_the_countdown_but_keeps_the_number():
    payload = _claude_payload(80, timedelta(minutes=-5), 40, timedelta(minutes=-5))
    text = quota.render_windows(quota.claude_windows(payload), NOW)

    assert text.startswith("5h: 80% (100%)")
    assert "0h 0m" not in text, "a finished window must not show a countdown"


def test_codex_windows_come_from_the_rate_limits_it_already_reports():
    rate_limit = {
        "primary": {"usedPercent": 99, "windowDurationMins": 300,
                    "resetsAt": int((NOW + timedelta(minutes=18, seconds=30)).timestamp())},
        "secondary": {"usedPercent": 15, "windowDurationMins": 10080,
                      "resetsAt": int((NOW + timedelta(days=6, hours=19, minutes=8)).timestamp())},
    }
    text = quota.render_windows(quota.codex_windows(rate_limit), NOW)

    assert text == ("5h: 99% (94%) 0h 18m · темп +16m\n"
                    "7d: 15% (3%) 6d 19h 8m · темп +20h 20m")


def test_an_unusual_window_length_is_labelled_by_its_duration():
    rate_limit = {"primary": {"usedPercent": 10, "windowDurationMins": 1440,
                              "resetsAt": None}}
    assert quota.render_windows(quota.codex_windows(rate_limit), NOW) == "1d: 10%"


def test_english_locale_translates_the_pace_label():
    payload = _claude_payload(99, timedelta(minutes=18, seconds=30), 15,
                              timedelta(days=6, hours=19, minutes=8))
    text = quota.render_windows(quota.claude_windows(payload), NOW, lang="en")

    assert "pace +16m" in text and "темп" not in text


def test_missing_or_malformed_windows_are_skipped_not_guessed():
    assert quota.claude_windows(None) == []
    assert quota.claude_windows({"five_hour": None, "seven_day_opus": None}) == []
    assert quota.claude_windows({"five_hour": {"utilization": None}}) == []
    assert quota.codex_windows({"primary": {"usedPercent": None}}) == []


# ---------- fetching ----------

class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, status=200, payload=None, raises=None):
        self._status, self._payload, self._raises = status, payload, raises
        self.headers = None

    def get(self, url, headers=None, timeout=None):
        self.headers = headers
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._status, self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """A credentials file plus a fake aiohttp — nothing may touch the network."""
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}))
    monkeypatch.setattr(quota, "fetch_claude_usage", _REAL_FETCH)
    monkeypatch.setattr(quota, "_credentials_path", lambda: creds)
    monkeypatch.setattr(quota, "_cache", None, raising=False)

    sessions = []

    def install(**kwargs):
        fake = pytypes.ModuleType("aiohttp")
        fake.ClientTimeout = lambda total=None: total

        def factory():
            session = _FakeSession(**kwargs)
            sessions.append(session)
            return session

        fake.ClientSession = factory
        monkeypatch.setitem(sys.modules, "aiohttp", fake)
        return sessions

    return install


@pytest.mark.asyncio
async def test_a_successful_fetch_returns_the_payload_and_sends_the_oauth_headers(offline):
    sessions = offline(payload={"five_hour": {"utilization": 3.0}})

    assert await quota.fetch_claude_usage() == {"five_hour": {"utilization": 3.0}}
    assert sessions[0].headers["Authorization"] == "Bearer tok-123"
    assert sessions[0].headers["anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.asyncio
async def test_an_expired_token_yields_none_instead_of_an_exception(offline):
    offline(status=401, payload={"error": "expired"})
    assert await quota.fetch_claude_usage() is None


@pytest.mark.asyncio
async def test_a_timeout_yields_none(offline):
    offline(raises=asyncio.TimeoutError())
    assert await quota.fetch_claude_usage() is None


@pytest.mark.asyncio
async def test_a_missing_credentials_file_yields_none(monkeypatch, tmp_path):
    monkeypatch.setattr(quota, "fetch_claude_usage", _REAL_FETCH)
    monkeypatch.setattr(quota, "_credentials_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(quota, "_cache", None, raising=False)
    assert await quota.fetch_claude_usage() is None


@pytest.mark.asyncio
async def test_the_result_is_cached_so_parallel_chats_do_not_hammer_the_api(offline):
    sessions = offline(payload={"five_hour": {"utilization": 3.0}})

    await asyncio.gather(*(quota.fetch_claude_usage() for _ in range(5)))
    assert len(sessions) == 1, f"{len(sessions)} HTTP round trips for one window"


@pytest.mark.asyncio
async def test_a_failure_is_cached_too(offline):
    sessions = offline(status=401, payload={})

    assert await quota.fetch_claude_usage() is None
    assert await quota.fetch_claude_usage() is None
    assert len(sessions) == 1, "a dead token cost a round trip per call"


# ---------- the block used by the bot ----------

@pytest.mark.asyncio
async def test_quota_block_reads_claude_over_http_and_codex_from_the_session(monkeypatch):
    payload = _claude_payload(99, timedelta(minutes=18, seconds=30),
                              15, timedelta(days=6, hours=19, minutes=8))

    async def fake_fetch():
        return payload

    monkeypatch.setattr(quota, "fetch_claude_usage", fake_fetch)

    class CodexSession:
        rate_limit = {"primary": {"usedPercent": 50, "windowDurationMins": 300,
                                  "resetsAt": None}}

    assert (await quota.quota_block("claude", object(), now=NOW)).startswith("5h: 99%")
    assert await quota.quota_block("codex", CodexSession(), now=NOW) == "5h: 50%"


@pytest.mark.asyncio
async def test_quota_block_never_raises_into_the_turn(monkeypatch):
    """This decorates failure messages — losing the message is worse than
    losing the numbers."""
    async def exploding():
        raise RuntimeError("boom")

    monkeypatch.setattr(quota, "fetch_claude_usage", exploding)
    assert await quota.quota_block("claude", object(), now=NOW) == ""
