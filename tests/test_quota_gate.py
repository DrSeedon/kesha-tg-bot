"""The remaining quota code is admission-only; presentation belongs to `/limits`."""

import asyncio
import json
import sys
import types as pytypes

import pytest

import quota_gate


def test_provider_windows_are_normalized_only_for_exhaustion():
    claude = quota_gate.claude_windows({
        "five_hour": {"utilization": 100},
        "seven_day": {"utilization": 12},
    })
    codex = quota_gate.codex_windows({
        "primary": {"usedPercent": 42},
        "secondary": {"usedPercent": 100},
    })

    assert claude == [{"utilization": 100.0}, {"utilization": 12.0}]
    assert codex == [{"utilization": 42.0}, {"utilization": 100.0}]
    assert quota_gate.quota_exhausted(claude)
    assert quota_gate.quota_exhausted(codex)
    assert not quota_gate.quota_exhausted([])


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
    credentials = tmp_path / ".credentials.json"
    credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(quota_gate, "_credentials_path", lambda: credentials)
    monkeypatch.setattr(quota_gate, "_cache", None)
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
async def test_successful_gate_fetch_uses_oauth_and_is_cached(offline):
    sessions = offline(payload={"five_hour": {"utilization": 3.0}})

    results = await asyncio.gather(*(quota_gate.fetch_claude_usage() for _ in range(5)))

    assert results == [{"five_hour": {"utilization": 3.0}}] * 5
    assert len(sessions) == 1
    assert sessions[0].headers["Authorization"] == "Bearer tok-123"
    assert sessions[0].headers["anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.asyncio
async def test_gate_fetch_fails_open_and_caches_the_failure(offline):
    sessions = offline(status=401, payload={})

    assert await quota_gate.fetch_claude_usage() is None
    assert await quota_gate.fetch_claude_usage() is None
    assert len(sessions) == 1
