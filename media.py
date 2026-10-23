"""Media utilities: download, transcribe, cache, cleanup, media_count, log_size."""

import asyncio
import json
import os
import re
import time
from pathlib import Path

from aiogram import types

from config import DEEPGRAM, LOG_DIR, MEDIA_DIR, MEDIA_MAX_AGE_H, MEDIA_MAX_MB, logger

_bot = None


def set_bot(bot_instance) -> None:
    global _bot
    _bot = bot_instance


MEDIA_DIR.mkdir(parents=True, exist_ok=True)

LOG_MAX_AGE_DAYS = 7

# --- Media cleanup ---

def cleanup_media():
    global _file_cache
    skip = (".cache.json", ".transcription_cache.json")
    files = []
    total_bytes = 0
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.name not in skip:
            st = f.stat()
            files.append((f, st.st_mtime, st.st_size))
            total_bytes += st.st_size
    limit_bytes = MEDIA_MAX_MB * 1024 * 1024
    if total_bytes <= limit_bytes:
        logger.info(f"Media cleanup: {total_bytes // (1024*1024)}MB / {MEDIA_MAX_MB}MB — all good")
        return
    files.sort(key=lambda x: x[1])
    cutoff = time.time() - MEDIA_MAX_AGE_H * 3600
    count = 0
    for f, mtime, size in files:
        if total_bytes <= limit_bytes:
            break
        if mtime < cutoff:
            f.unlink()
            total_bytes -= size
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old media files ({total_bytes // (1024*1024)}MB remaining, limit {MEDIA_MAX_MB}MB)")
        _file_cache = {k: v for k, v in _file_cache.items() if Path(v).exists()}
        _save_cache(_file_cache)
    elif total_bytes > limit_bytes:
        logger.warning(f"Media {total_bytes // (1024*1024)}MB > {MEDIA_MAX_MB}MB but no files older than {MEDIA_MAX_AGE_H}h to delete")


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


# Deepgram recommends ~0.65 for high-precision error detection:
# https://developers.deepgram.com/docs/confidence
LOW_WORD_CONFIDENCE = 0.65
UNCERTAIN_TRANSCRIPT_HEADER = "[Расшифровка содержит неуверенно распознанные слова: [?]]"


def _format_transcript(alternative: dict) -> str:
    """Annotate word confidence without replacing Deepgram's best guess."""
    text = alternative["transcript"]
    words = alternative.get("words") or []
    uncertain = [
        isinstance(word.get("confidence"), (int, float))
        and not isinstance(word["confidence"], bool)
        and 0 <= word["confidence"] < LOW_WORD_CONFIDENCE
        for word in words
    ]
    if not text or not any(uncertain):
        return text

    # Prefer the provider's formatted tokens (casing, punctuation, numerals).
    # Keep the original whitespace when tokens align with the transcript.
    tokens = [word.get("punctuated_word") or word["word"] for word in words]
    marked = []
    for token, low_confidence in zip(tokens, uncertain):
        if low_confidence:
            # Place the marker before closing quotes and sentence punctuation.
            token = re.sub(r"(\W*)$", r"[?]\1", token, count=1)
        marked.append(token)

    pieces = []
    cursor = 0
    for token, annotated in zip(tokens, marked):
        match = re.compile(r"(?<!\w)" + re.escape(token) + r"(?!\w)").search(text, cursor)
        if match is None:
            # Smart Format may combine spoken words into an entity. In that
            # case retain every word and its marker, rather than guess a span.
            body = " ".join(marked)
            break
        start = match.start()
        pieces.extend((text[cursor:start], annotated))
        cursor = start + len(token)
    else:
        pieces.append(text[cursor:])
        body = "".join(pieces)
    return f"{UNCERTAIN_TRANSCRIPT_HEADER}\n{body}"


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
                "https://api.deepgram.com/v1/listen?model=nova-3&language=ru&smart_format=true",
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
        alternative = data["results"]["channels"][0]["alternatives"][0]
        text = _format_transcript(alternative)
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
