"""Media utilities: download, transcribe, cache, cleanup, media_count, log_size."""

import asyncio
import json
import os
import time
from pathlib import Path

from aiogram import types

from config import DEEPGRAM, LOG_DIR, MEDIA_DIR, MEDIA_MAX_AGE_H, logger

_bot = None


def set_bot(bot_instance) -> None:
    global _bot
    _bot = bot_instance


MEDIA_DIR.mkdir(parents=True, exist_ok=True)

LOG_MAX_AGE_DAYS = 7

# --- Media cleanup ---

def cleanup_media():
    global _file_cache
    cutoff = time.time() - MEDIA_MAX_AGE_H * 3600
    count = 0
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.name not in (".cache.json", ".transcription_cache.json") and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old media files")
        _file_cache = {k: v for k, v in _file_cache.items() if Path(v).exists()}
        _save_cache(_file_cache)


def cleanup_logs():
    """Delete log backup files older than LOG_MAX_AGE_DAYS."""
    cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
    count = 0
    for f in LOG_DIR.iterdir():
        if f.is_file() and f.name.startswith("kesha.log.") and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old log files (>{LOG_MAX_AGE_DAYS}d)")


async def daily_cleanup_loop():
    """Run media + log cleanup every 24h while bot is alive."""
    while True:
        await asyncio.sleep(86400)
        try:
            cleanup_media()
            cleanup_logs()
        except Exception as e:
            logger.error(f"Daily cleanup error: {e}")


def media_count() -> int:
    return sum(1 for f in MEDIA_DIR.iterdir() if f.is_file())


def log_size() -> str:
    p = LOG_DIR / "kesha.log"
    if not p.exists():
        return "0 KB"
    size = p.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size // 1024} KB"
    return f"{size // (1024 * 1024)} MB"


# --- Transcription cache ---

TRANSCRIPTION_CACHE_FILE = MEDIA_DIR / ".transcription_cache.json"
_transcription_cache: dict[str, str] = {}


def _load_transcription_cache() -> dict[str, str]:
    if TRANSCRIPTION_CACHE_FILE.exists():
        try:
            return json.loads(TRANSCRIPTION_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_transcription_cache():
    TRANSCRIPTION_CACHE_FILE.write_text(json.dumps(_transcription_cache, ensure_ascii=False))


_transcription_cache = _load_transcription_cache()


async def transcribe(path: str, unique_id: str = "") -> tuple[str, str | None]:
    if unique_id and unique_id in _transcription_cache:
        cached = _transcription_cache[unique_id]
        logger.info(f"Transcription cache hit: {unique_id} ({len(cached)} chars)")
        return cached, None

    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as _http:
            with open(path, "rb") as _af:
                audio_data = _af.read()
            async with _http.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=ru&smart_format=true",
                headers={"Authorization": f"Token {DEEPGRAM}", "Content-Type": "audio/ogg"},
                data=audio_data,
                timeout=_aiohttp.ClientTimeout(total=120),
            ) as resp:
                out = await resp.read()
    except Exception as e:
        logger.error(f"Deepgram request error: {e}")
        return "", str(e)
    try:
        data = json.loads(out)
        if "error" in data:
            return "", data["error"]
        if "err_msg" in data:
            return "", data["err_msg"]
        text = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        duration = data.get("metadata", {}).get("duration", 0)
        cost = duration / 60 * 0.0043
        logger.info(f"Deepgram: {duration:.1f}s, ${cost:.4f}, {len(text)} chars")
        if unique_id and text:
            _transcription_cache[unique_id] = text
            _save_transcription_cache()
            logger.info(f"Transcription cached: {unique_id}")
        return text, None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raw = out.decode(errors="replace")[:200]
        logger.error(f"Deepgram parse error: {e}, raw: {raw}")
        return "", str(e)


# --- File download cache ---

CACHE_FILE = MEDIA_DIR / ".cache.json"


def _load_cache() -> dict[str, str]:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            return {k: v for k, v in data.items() if Path(v).exists()}
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, str]):
    CACHE_FILE.write_text(json.dumps(cache))


_file_cache: dict[str, str] = _load_cache()


async def download_file(file_id: str, name: str, unique_id: str = "") -> str | None:
    if unique_id and unique_id in _file_cache:
        cached = _file_cache[unique_id]
        if Path(cached).exists():
            logger.info(f"Cache hit: {unique_id} → {Path(cached).name}")
            return cached
        del _file_cache[unique_id]
    try:
        f = await _bot.get_file(file_id)
        name = Path(name).name
        path = MEDIA_DIR / name
        if path.exists():
            stem = path.stem
            suffix = path.suffix
            i = 1
            while path.exists():
                path = MEDIA_DIR / f"{stem}_{i}{suffix}"
                i += 1
        await _bot.download_file(f.file_path, str(path))
        if unique_id:
            _file_cache[unique_id] = str(path)
            _save_cache(_file_cache)
        return str(path)
    except Exception as e:
        logger.warning(f"download_file failed for {name}: {e}")
        return None


def _media_name(prefix: str, ext: str, msg: types.Message) -> str:
    ts = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else str(msg.message_id)
    return f"{prefix}_{ts}_{msg.message_id}{ext}"
