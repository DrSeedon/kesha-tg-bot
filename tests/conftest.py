import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quota  # noqa: E402


@pytest.fixture(autouse=True)
def no_quota_network(monkeypatch):
    """The quota block is now rendered on the limit path, which many tests walk.

    Without this the suite would reach api.anthropic.com with the developer's
    real token. Tests that want windows patch `fetch_claude_usage` themselves.
    """
    async def _offline():
        return None

    monkeypatch.setattr(quota, "fetch_claude_usage", _offline)
