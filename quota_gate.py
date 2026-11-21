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
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from pathlib import Path as _Path
import time as _time

ANTIGRAVITY_COOLDOWN_PATH = _Path(
    os.getenv(
        "KESHA_ANTIGRAVITY_COOLDOWN_PATH",
        "/opt/kesha-bot/storage/antigravity-cooldown.json",
    )
)
ANTIGRAVITY_COOLDOWN_FALLBACK_SECONDS = 900
ANTIGRAVITY_COOLDOWN_BUFFER_SECONDS = 30
ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS = 900
_AGY_QUOTA_ERR_RE = _re.compile(
    r"individual\s+quota\s+reached|quota\s+(?:limit\s+)?reached|quota\s+exhausted|resource_exhausted|\b429\b",
    _re.IGNORECASE,
)
_AGY_RESETS_IN_RE = _re.compile(r"resets?\s+in\s+([^\n.;,]+)", _re.IGNORECASE)
_AGY_DURATION_RE = _re.compile(r"(\d+)\s*([dhms])", _re.IGNORECASE)


def pool_for_model(model: object = None) -> str:
    """Map an Antigravity model id to its independent quota pool: gemini | argon | 3p."""
    raw = str(model or "").strip().casefold()
    if raw.startswith("antigravity/"):
        raw = raw[len("antigravity/"):]
    if "argon" in raw or "g1-pro" in raw:
        return "argon"
    if raw.startswith(("claude", "gpt-oss", "anthropic")):
        return "3p"
    return "gemini"


def _format_krs_hhmm(ts: float) -> str:
    return _dt.fromtimestamp(float(ts), tz=_tz(_td(hours=7))).strftime("%H:%M")


def antigravity_cooldown_message(until_ts: float, *, pool: str | None = None) -> str:
    pool_tag = f" ({pool})" if pool else ""
    return (
        f"⛔ Antigravity{pool_tag} лимит исчерпан — антибан-стоп до "
        f"{_format_krs_hhmm(until_ts)} КРС (переключите модель или рантайм: /gemini, /argon, /codex)"
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
    capped_seconds = min(reset_seconds + ANTIGRAVITY_COOLDOWN_BUFFER_SECONDS, ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS)
    until_ts = now_ts + capped_seconds
    return True, until_ts, reset_seconds


def _load_cooldown_pools() -> dict[str, dict]:
    try:
        if not ANTIGRAVITY_COOLDOWN_PATH.is_file():
            return {}
        data = _json.loads(ANTIGRAVITY_COOLDOWN_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        pools = data.get("pools")
        if isinstance(pools, dict):
            return {
                str(k): dict(v)
                for k, v in pools.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
        # Legacy flat file support: treat as "gemini" pool if valid.
        if "until" in data:
            return {"gemini": dict(data)}
        return {}
    except Exception as exc:
        logger.warning(f"quota gate: cooldown read failed ({ANTIGRAVITY_COOLDOWN_PATH}): {exc}")
        return {}


def _write_cooldown_pools(pools: dict[str, dict]) -> None:
    if not pools:
        clear_antigravity_cooldown()
        return
    try:
        ANTIGRAVITY_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ANTIGRAVITY_COOLDOWN_PATH.with_name(f".{ANTIGRAVITY_COOLDOWN_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(_json.dumps({"pools": pools}, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(ANTIGRAVITY_COOLDOWN_PATH)
        os.chmod(ANTIGRAVITY_COOLDOWN_PATH, 0o600)
    except Exception as exc:
        logger.warning(f"quota gate: cooldown write failed ({ANTIGRAVITY_COOLDOWN_PATH}): {exc}")


def antigravity_cooldown_status(
    *, model: object = None, now: float | None = None
) -> dict | None:
    now_ts = _time.time() if now is None else float(now)
    pool = pool_for_model(model)
    entry = _load_cooldown_pools().get(pool)
    if not isinstance(entry, dict):
        return None
    try:
        until_ts = float(entry.get("until"))
    except (TypeError, ValueError):
        return None
    if not _math.isfinite(until_ts):
        return None
    tripped_raw = entry.get("tripped_at")
    if isinstance(tripped_raw, (int, float)) and _math.isfinite(float(tripped_raw)):
        until_ts = min(until_ts, float(tripped_raw) + ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS)
    else:
        until_ts = min(until_ts, now_ts + ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS)
    if until_ts <= now_ts:
        return None
    reason = str(entry.get("reason") or "")
    return {
        "active": True,
        "pool": pool,
        "until": until_ts,
        "until_krs": _format_krs_hhmm(until_ts),
        "remaining_seconds": max(1, int(_math.ceil(until_ts - now_ts))),
        "reason": reason,
        "message": antigravity_cooldown_message(until_ts, pool=pool),
    }


def record_antigravity_cooldown(
    raw_message: object,
    *,
    until_ts: float | None = None,
    now: float | None = None,
    model: object = None,
) -> dict:
    now_ts = _time.time() if now is None else float(now)
    pool = pool_for_model(model)
    parsed_until = until_ts
    if parsed_until is None:
        _, parsed_until, _ = parse_antigravity_quota_cooldown(raw_message, now=now_ts)
    if parsed_until is None or parsed_until <= now_ts:
        parsed_until = now_ts + min(
            ANTIGRAVITY_COOLDOWN_FALLBACK_SECONDS + ANTIGRAVITY_COOLDOWN_BUFFER_SECONDS,
            ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS,
        )
    parsed_until = min(float(parsed_until), now_ts + ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS)

    pools = {
        k: v
        for k, v in _load_cooldown_pools().items()
        if isinstance(v.get("until"), (int, float)) and float(v["until"]) > now_ts
    }
    existing = pools.get(pool)
    if isinstance(existing, dict) and isinstance(existing.get("until"), (int, float)):
        prev_u = min(float(existing["until"]), now_ts + ANTIGRAVITY_COOLDOWN_MAX_TTL_SECONDS)
        if prev_u > parsed_until:
            parsed_until = prev_u

    record = {
        "until": parsed_until,
        "tripped_at": now_ts,
        "reason": str(raw_message or "Individual quota reached")[:300],
    }
    pools[pool] = record
    _write_cooldown_pools(pools)
    return {
        "active": True,
        "pool": pool,
        "until": parsed_until,
        "until_krs": _format_krs_hhmm(parsed_until),
        "remaining_seconds": max(1, int(_math.ceil(parsed_until - now_ts))),
        "reason": record["reason"],
        "message": antigravity_cooldown_message(parsed_until, pool=pool),
    }


def clear_antigravity_cooldown(model: object = None) -> None:
    if model is not None:
        pool = pool_for_model(model)
        pools = _load_cooldown_pools()
        if pool in pools:
            pools.pop(pool, None)
            _write_cooldown_pools(pools)
        return
    try:
        if ANTIGRAVITY_COOLDOWN_PATH.is_file():
            ANTIGRAVITY_COOLDOWN_PATH.unlink()
    except Exception as exc:
        logger.warning(f"quota gate: cooldown clear failed ({ANTIGRAVITY_COOLDOWN_PATH}): {exc}")


_AGY_QUOTA_PREFIX_MARKERS = (
    "individual quota reached",
    "quota reached",
    "quota limit reached",
    "quota exhausted",
    "resource_exhausted",
)


def looks_like_antigravity_quota_text(
    text: object, *, runtime: str, tools: int = 0, now: float | None = None
) -> tuple[bool, float | None, int | None]:
    """Detect raw Antigravity quota refusal emitted as assistant prose instead of error chunk."""
    if runtime != "antigravity" or tools > 0:
        return False, None, None
    raw = str(text or "").strip()
    if not raw or len(raw) > 500:
        return False, None, None
    matched, until_ts, reset_s = parse_antigravity_quota_cooldown(raw, now=now)
    if not matched:
        return False, None, None
    low = raw.casefold()
    if (
        "individual quota reached" in low
        or "resets in" in low
        or "upgrade your subscription" in low
        or "quota reached" in low
        or "quota limit reached" in low
        or "quota exhausted" in low
        or "resource_exhausted" in low
        or raw.startswith("⚠")
    ):
        return True, until_ts, reset_s
    return False, None, None
