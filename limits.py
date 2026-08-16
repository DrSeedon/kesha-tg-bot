"""Canonical subscription limits supplied by the local Orchestra service.

Orchestra owns provider polling, five-minute history and Claude's measured
5h↔7d exchange rate. Kesha only authenticates to that local API, formats the
Telegram caption and forwards Orchestra's canonical PNG unchanged.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from dotenv import dotenv_values


DEFAULT_ORCHESTRA_URL = "http://127.0.0.1:8888"
DEFAULT_ORCHESTRA_ENV_FILE = "/home/kesha/orchestra/.env"
USAGE_TIMEOUT_SECONDS = 15
CARD_TIMEOUT_SECONDS = 30
PACE_OK_DELTA = 5.0
WINDOW_FALLBACK_MINUTES = {"five_hour": 300, "seven_day": 10080}


class LimitsUnavailable(RuntimeError):
    """Orchestra could not supply an authoritative limits view."""


def orchestra_token() -> str:
    """Read the shared internal token without copying it into Kesha's files."""
    explicit = os.getenv("ORCHESTRA_INTERNAL_TOKEN", "").strip()
    if explicit:
        return explicit
    env_path = Path(os.getenv("ORCHESTRA_ENV_FILE", DEFAULT_ORCHESTRA_ENV_FILE))
    try:
        token = str(dotenv_values(env_path).get("INTERNAL_TOKEN") or "").strip()
    except (OSError, ValueError) as exc:
        raise LimitsUnavailable(
            f"не удалось прочитать Orchestra env: {type(exc).__name__}"
        ) from exc
    if not token:
        raise LimitsUnavailable("INTERNAL_TOKEN Orchestra не настроен")
    return token


def _orchestra_url(path: str) -> str:
    base = os.getenv("ORCHESTRA_URL", DEFAULT_ORCHESTRA_URL).rstrip("/")
    return f"{base}{path}"


async def fetch_limits_usage() -> dict:
    headers = {"Authorization": f"Bearer {orchestra_token()}"}
    timeout = aiohttp.ClientTimeout(total=USAGE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_orchestra_url("/api/usage"), headers=headers) as response:
            if response.status != 200:
                raise LimitsUnavailable(f"Orchestra /api/usage: HTTP {response.status}")
            payload = await response.json()
            if not isinstance(payload, dict):
                raise LimitsUnavailable("Orchestra /api/usage вернула не объект")
            return payload


async def fetch_limits_card() -> bytes:
    headers = {"Authorization": f"Bearer {orchestra_token()}"}
    timeout = aiohttp.ClientTimeout(total=CARD_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_orchestra_url("/api/usage/card"), headers=headers) as response:
            if response.status != 200:
                raise LimitsUnavailable(f"Orchestra /api/usage/card: HTTP {response.status}")
            image = await response.read()
            if not image:
                raise LimitsUnavailable("Orchestra вернула пустую карточку лимитов")
            return image


def _to_utc_datetime(raw) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_minutes(window_id: str | None, window: dict) -> int | None:
    minutes = window.get("window_minutes")
    if isinstance(minutes, int) and minutes > 0:
        return minutes
    return WINDOW_FALLBACK_MINUTES.get(window_id) if window_id else None


def _progress_pct(reset: datetime, window_minutes: int | None, now: datetime) -> int | None:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return None
    remaining = (reset - now).total_seconds()
    elapsed = window_minutes * 60 - remaining
    return int(max(0, min(100, round(elapsed / (window_minutes * 60) * 100))))


def _pace_text(
    utilization: float,
    reset: datetime,
    window_minutes: int | None,
    now: datetime,
) -> str:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return "темп не известен"
    remaining_ms = max(0, int((reset - now).total_seconds() * 1000))
    elapsed_ms = window_minutes * 60_000 - remaining_ms
    delta = utilization - elapsed_ms / (window_minutes * 60_000) * 100
    if delta <= PACE_OK_DELTA:
        return "темп ok"
    cooldown_min = round(delta * window_minutes / 100)
    if cooldown_min < 60:
        label = f"{cooldown_min}m"
    elif cooldown_min < 1440:
        label = f"{cooldown_min // 60}ч {cooldown_min % 60}м"
    else:
        days, rest = divmod(cooldown_min, 1440)
        label = f"{days}д {rest // 60}ч {rest % 60}м"
    return f"темп +{label}"


def format_limits_message(usage: dict, *, now: datetime | None = None) -> str:
    """Telegram caption kept byte-for-byte compatible with Orchestra `/limits`."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_tz = timezone(timedelta(hours=7))

    def fmt_pct(value: float) -> str:
        return f"{max(0.0, min(100.0, value)):.1f}".rstrip("0").rstrip(".")

    def reset_countdown(reset: datetime) -> str:
        seconds = int((reset - now).total_seconds())
        if seconds <= 0:
            return "сброс уже наступил"
        minutes_total = max(1, int((seconds + 59) // 60))
        days, minutes_total = divmod(minutes_total, 24 * 60)
        hours, minutes = divmod(minutes_total, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days} д")
        if hours:
            parts.append(f"{hours} ч")
        if minutes or not parts:
            parts.append(f"{minutes} мин")
        return f"через {' '.join(parts)}"

    def window_line(label: str, window: dict | None, window_id: str | None = None) -> str:
        if not isinstance(window, dict) or not isinstance(
            window.get("utilization"), (int, float)
        ):
            return f"• {label} — нет данных"
        utilization = float(window["utilization"])
        remaining = 100.0 - utilization
        consumed_text = fmt_pct(utilization)
        remaining_text = fmt_pct(remaining)
        reset = _to_utc_datetime(window.get("resets_at"))
        if reset is None:
            return (
                f"• {label} — осталось {remaining_text}%; "
                f"израсходовано {consumed_text}% (окно не указано); сброс не указан"
            )
        absolute = reset.astimezone(local_tz).strftime("%d.%m.%Y %H:%M UTC+7")
        minutes = _window_minutes(window_id, window)
        progress = _progress_pct(reset, minutes, now)
        pace = _pace_text(utilization, reset, minutes, now)
        parts = [f"• {label} — осталось {remaining_text}%; израсходовано {consumed_text}%"]
        if progress is not None:
            parts.append(f"окно ({progress}%)")
        parts.append(f"сброс {absolute}, {reset_countdown(reset)}")
        parts.append(pace)
        return "; ".join(parts)

    anthropic = usage.get("anthropic") or {}
    codex = usage.get("codex") or {}
    lines = [
        "*Лимиты*",
        window_line("Claude 5h", anthropic.get("five_hour"), "five_hour"),
        window_line("Claude 7d", anthropic.get("seven_day"), "seven_day"),
        window_line("Codex", codex.get("primary")),
        window_line("Spark", (codex.get("spark") or {}).get("primary")),
        window_line("Grok", (usage.get("grok") or {}).get("primary")),
    ]
    extra_usage = anthropic.get("extra_usage") or {}
    if extra_usage.get("spend_limit_reached") is True:
        lines.append(
            "• Claude extra usage — лимит расходов достигнут "
            "(базовые окна считаются отдельно)"
        )
    return "\n".join(lines)
