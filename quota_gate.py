"""Minimal provider quota admission checks.

User-facing limits live in Orchestra and are exposed by Kesha through
`/limits`. This module deliberately keeps only the fresh Claude read and the
small normalization needed to refuse a turn after a provider reaches 100%.
"""

import asyncio
import json
import os
from pathlib import Path
from time import monotonic
from typing import Any, Optional

from config import logger


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_TTL_SEC = 60.0
FETCH_TIMEOUT_SEC = 10.0

_fetch_lock = asyncio.Lock()
_cache: Optional[tuple[float, Optional[dict]]] = None


def _credentials_path() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def _access_token() -> Optional[str]:
    try:
        data = json.loads(_credentials_path().read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"quota gate: credentials unreadable: {type(exc).__name__}: {exc}")
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken") or None


async def _fetch_usage_uncached() -> Optional[dict]:
    token = _access_token()
    if not token:
        return None
    import aiohttp

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SEC),
            ) as response:
                if response.status != 200:
                    logger.warning(f"quota gate: usage endpoint returned HTTP {response.status}")
                    return None
                payload = await response.json()
                return payload if isinstance(payload, dict) else None
    except Exception as exc:
        logger.warning(f"quota gate: usage fetch failed: {type(exc).__name__}: {exc}")
        return None


async def fetch_claude_usage(*, force: bool = False) -> Optional[dict]:
    """Cached Claude usage payload. Admission remains fail-open when unavailable."""
    global _cache
    async with _fetch_lock:
        if (
            not force
            and _cache is not None
            and monotonic() - _cache[0] < CACHE_TTL_SEC
        ):
            return _cache[1]
        payload = await _fetch_usage_uncached()
        _cache = (monotonic(), payload)
        return payload


def _window(utilization: Any) -> Optional[dict]:
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
        return None
    return {"utilization": float(utilization)}


def claude_windows(payload: Optional[dict]) -> list[dict]:
    if not payload:
        return []
    windows = []
    for key in ("five_hour", "seven_day"):
        raw = payload.get(key)
        if not isinstance(raw, dict):
            continue
        window = _window(raw.get("utilization"))
        if window:
            windows.append(window)
    return windows


def codex_windows(rate_limit: Optional[dict]) -> list[dict]:
    if not rate_limit:
        return []
    windows = []
    for key in ("primary", "secondary"):
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        window = _window(raw.get("usedPercent"))
        if window:
            windows.append(window)
    return windows


def quota_exhausted(windows: list[dict]) -> bool:
    return any(window["utilization"] >= 100.0 for window in windows)
