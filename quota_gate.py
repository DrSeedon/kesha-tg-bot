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

import json as _json
import math as _math
import re as _re
import sqlite3 as _sqlite3
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from pathlib import Path as _Path
import time as _time

ANTIGRAVITY_COOLDOWN_PATH = _Path(
    os.getenv("KESHA_ANTIGRAVITY_COOLDOWN_PATH", "/tmp/kesha-antigravity-cooldown.json")
)
ORCHESTRA_DB_PATH = _Path(
    os.getenv("ORCHESTRA_DB_PATH", "/home/kesha/orchestra/data/orchestra.db")
)
ANTIGRAVITY_COOLDOWN_FALLBACK_SECONDS = 1800
ANTIGRAVITY_COOLDOWN_BUFFER_SECONDS = 90
_AGY_QUOTA_ERR_RE = _re.compile(
    r"individual\s+quota\s+reached|quota\s+(?:limit\s+)?reached|quota\s+exhausted|resource_exhausted|\b429\b",
    _re.IGNORECASE,
)
_AGY_RESETS_IN_RE = _re.compile(r"resets?\s+in\s+([^\n.;,]+)", _re.IGNORECASE)
_AGY_DURATION_RE = _re.compile(r"(\d+)\s*([dhms])", _re.IGNORECASE)


def _format_krs_hhmm(ts: float) -> str:
    return _dt.fromtimestamp(float(ts), tz=_tz(_td(hours=7))).strftime("%H:%M")


def antigravity_cooldown_message(until_ts: float) -> str:
    return (
        f"⛔ Antigravity 5h лимит исчерпан — антибан-стоп до "
        f"{_format_krs_hhmm(until_ts)} КРС (переключите на gpt-6.2-sol)"
    )


def parse_antigravity_quota_cooldown(
    text: object, *, now: float | None = None
) -> tuple[bool, float | None, int | None]:
    raw = str(text or "").strip()
    if not raw or not _AGY_QUOTA_ERR_RE.search(raw):
        return False, None, None
    now_ts = _time.time() if now is None else float(now)
    reset_seconds = 0
    m = _AGY_RESETS_IN_RE.search(raw)
    if m:
        for amount_s, unit in _AGY_DURATION_RE.findall(m.group(1)):
            amt = int(amount_s)
            u = unit.lower()
            reset_seconds += amt * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
    if reset_seconds <= 0:
        reset_seconds = ANTIGRAVITY_COOLDOWN_FALLBACK_SECONDS
    until_ts = now_ts + reset_seconds + ANTIGRAVITY_COOLDOWN_BUFFER_SECONDS
    return True, until_ts, reset_seconds


def _read_orchestra_antigravity_cooldown() -> float | None:
    try:
        if not ORCHESTRA_DB_PATH.is_file():
            return None
        with _sqlite3.connect(f"file:{ORCHESTRA_DB_PATH}?mode=ro", uri=True, timeout=0.5) as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE key='antigravity_quota_cooldown'"
            ).fetchone()
        if not row or not row[0]:
            return None
        data = _json.loads(row[0])
        val = float(data.get("until"))
        return val if _math.isfinite(val) else None
    except Exception:
        return None


def antigravity_cooldown_status(*, now: float | None = None) -> dict | None:
    now_ts = _time.time() if now is None else float(now)
    candidates: list[tuple[float, str]] = []
    try:
        if ANTIGRAVITY_COOLDOWN_PATH.is_file():
            data = _json.loads(ANTIGRAVITY_COOLDOWN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                u = float(data.get("until"))
                if _math.isfinite(u) and u > now_ts:
                    candidates.append((u, str(data.get("reason") or "")))
    except Exception:
        pass
    orc_until = _read_orchestra_antigravity_cooldown()
    if orc_until is not None and orc_until > now_ts:
        candidates.append((orc_until, "orchestra_kv"))
    if not candidates:
        return None
    until_ts, reason = max(candidates, key=lambda x: x[0])
    return {
        "active": True,
        "until": until_ts,
        "until_krs": _format_krs_hhmm(until_ts),
        "remaining_seconds": max(1, int(_math.ceil(until_ts - now_ts))),
        "reason": reason,
        "message": antigravity_cooldown_message(until_ts),
    }


def record_antigravity_cooldown(
    raw_message: object, *, until_ts: float | None = None, now: float | None = None
) -> dict:
    now_ts = _time.time() if now is None else float(now)
    parsed_until = until_ts
    if parsed_until is None:
        _, parsed_until, _ = parse_antigravity_quota_cooldown(raw_message, now=now_ts)
    if parsed_until is None or parsed_until <= now_ts:
        parsed_until = now_ts + ANTIGRAVITY_COOLDOWN_FALLBACK_SECONDS + ANTIGRAVITY_COOLDOWN_BUFFER_SECONDS
    existing = antigravity_cooldown_status(now=now_ts)
    if existing and float(existing["until"]) > parsed_until:
        parsed_until = float(existing["until"])
    record = {
        "until": parsed_until,
        "tripped_at": now_ts,
        "reason": str(raw_message or "Individual quota reached")[:300],
    }
    try:
        ANTIGRAVITY_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ANTIGRAVITY_COOLDOWN_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ANTIGRAVITY_COOLDOWN_PATH)
    except Exception:
        pass
    return {
        "active": True,
        "until": parsed_until,
        "until_krs": _format_krs_hhmm(parsed_until),
        "remaining_seconds": max(1, int(_math.ceil(parsed_until - now_ts))),
        "reason": record["reason"],
        "message": antigravity_cooldown_message(parsed_until),
    }


def clear_antigravity_cooldown() -> None:
    try:
        if ANTIGRAVITY_COOLDOWN_PATH.is_file():
            ANTIGRAVITY_COOLDOWN_PATH.unlink()
    except Exception:
        pass

