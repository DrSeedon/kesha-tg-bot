"""Subscription quota windows — the real numbers behind "limit reached".

A bare "wait for the reset" tells the user nothing: which window is out, how
long is left, whether the burn is ahead of the window. Both runtimes can
answer that, from different sources — Claude over `oauth/usage`, Codex from
the rate limits it already reports — so the numbers are normalized into one
shape and rendered by one function.

Formatting mirrors the Orchestra dashboard (`app/static/js/usage.js`) so the
two never disagree about what "темп +16m" means.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Optional

from config import logger, render

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_TTL_SEC = 60.0
FETCH_TIMEOUT_SEC = 10.0
PACE_TOLERANCE_PCT = 5.0

_FIVE_HOURS_MIN = 300
_SEVEN_DAYS_MIN = 10080

_fetch_lock = asyncio.Lock()
_cache: Optional[tuple[float, Optional[dict]]] = None


# --- Claude: OAuth usage endpoint ---

def _credentials_path() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def _access_token() -> Optional[str]:
    try:
        data = json.loads(_credentials_path().read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"quota: credentials unreadable: {type(exc).__name__}: {exc}")
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
            ) as resp:
                if resp.status != 200:
                    # 401 means the CLI has to refresh the token; it does that on
                    # its own schedule. Nothing to do here but stay quiet.
                    logger.warning(f"quota: usage endpoint returned HTTP {resp.status}")
                    return None
                return await resp.json()
    except Exception as exc:
        logger.warning(f"quota: usage fetch failed: {type(exc).__name__}: {exc}")
        return None


async def fetch_claude_usage(*, force: bool = False) -> Optional[dict]:
    """Cached `oauth/usage` payload, or None. Never raises.

    Failures are cached too: a stale token would otherwise mean one dead HTTP
    round trip per message, and every chat asks at the same moment.
    """
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


# --- Normalization ---

def _parse_reset(value: Any) -> Optional[datetime]:
    """Claude sends ISO-8601, Codex sends unix seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _window_label(minutes: Any) -> str:
    if minutes == _FIVE_HOURS_MIN:
        return "5h"
    if minutes == _SEVEN_DAYS_MIN:
        return "7d"
    if not isinstance(minutes, int) or minutes <= 0:
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _window(label: str, utilization: Any, resets_at: Any, minutes: Any) -> Optional[dict]:
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
        return None
    return {
        "label": label,
        "utilization": float(utilization),
        "resets_at": _parse_reset(resets_at),
        "window_minutes": minutes if isinstance(minutes, int) and minutes > 0 else None,
    }


def claude_windows(payload: Optional[dict]) -> list[dict]:
    if not payload:
        return []
    out = []
    for key, minutes in (("five_hour", _FIVE_HOURS_MIN), ("seven_day", _SEVEN_DAYS_MIN)):
        raw = payload.get(key)
        if not isinstance(raw, dict):
            continue
        window = _window(_window_label(minutes), raw.get("utilization"),
                         raw.get("resets_at"), minutes)
        if window:
            out.append(window)
    return out


def codex_windows(rate_limit: Optional[dict]) -> list[dict]:
    if not rate_limit:
        return []
    out = []
    for key in ("primary", "secondary"):
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        minutes = raw.get("windowDurationMins")
        window = _window(_window_label(minutes), raw.get("usedPercent"),
                         raw.get("resetsAt"), minutes)
        if window:
            out.append(window)
    return out


def quota_exhausted(windows: list[dict]) -> bool:
    """A fresh provider window at 100% cannot admit another model turn."""
    return any(window["utilization"] >= 100.0 for window in windows)


# --- Rendering (pure: `now` is an argument, never read from the clock) ---

def _countdown(remaining_sec: float) -> str:
    if remaining_sec <= 0:
        return ""
    hours, minutes = divmod(int(remaining_sec // 60), 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes // 1440}d {(minutes % 1440) // 60}h {minutes % 60}m"


def _pace(utilization: float, elapsed_min: float, window_min: int, lang: str) -> str:
    """How far ahead of a linear burn we are, expressed as idle time owed."""
    delta = utilization - elapsed_min / window_min * 100
    if delta <= PACE_TOLERANCE_PCT:
        return render("quota_pace_ok", lang)
    return render("quota_pace_over", lang,
                  value=_duration(round(delta * window_min / 100)))


def render_window(window: dict, now: datetime, lang: str = "ru") -> str:
    line = f"{window['label']}: {round(window['utilization'])}%"
    resets_at, window_min = window["resets_at"], window["window_minutes"]
    if not resets_at or not window_min:
        return line
    # Clamping remaining into the window keeps a stale or absurd reset time from
    # producing a negative "elapsed" and a nonsense pace.
    remaining_sec = min(max((resets_at - now).total_seconds(), 0.0), window_min * 60)
    elapsed_min = window_min - remaining_sec / 60
    line += f" ({round(elapsed_min / window_min * 100)}%)"
    countdown = _countdown(remaining_sec)
    if countdown:
        line += f" {countdown}"
    return f"{line} · {_pace(window['utilization'], elapsed_min, window_min, lang)}"


def render_windows(windows: list[dict], now: datetime, lang: str = "ru") -> str:
    return "\n".join(render_window(w, now, lang) for w in windows)


async def quota_block(runtime_id: str, session: Any, lang: str = "ru",
                      now: Optional[datetime] = None) -> str:
    """Rendered windows for the chat's runtime, or "" when unavailable.

    Never raises: this decorates messages that explain a failure, and losing
    the explanation to a quota lookup would be worse than losing the numbers.
    """
    try:
        if runtime_id == "codex":
            windows = codex_windows(getattr(session, "rate_limit", None))
        else:
            windows = claude_windows(await fetch_claude_usage())
        return render_windows(windows, now or datetime.now(timezone.utc), lang)
    except Exception as exc:
        logger.warning(f"quota: block unavailable: {type(exc).__name__}: {exc}")
        return ""
