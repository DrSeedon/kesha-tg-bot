"""Word-confidence annotations and offline Deepgram/cache integration."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import media


HEADER = "[Расшифровка содержит неуверенно распознанные слова: [?]]"


@pytest.mark.parametrize("words", [None, [], [{"word": "Привет", "confidence": 0.99}]])
def test_confident_transcript_is_unchanged(words):
    alternative = {"transcript": "  Привет!\n"}
    if words is not None:
        alternative["words"] = words
    assert media._format_transcript(alternative) == "  Привет!\n"


@pytest.mark.parametrize(
    ("confidence", "marked"),
    [
        (0, True), (0.64, True), (0.649999, True), (0.65, False), (1, False),
        (None, False), (False, False), (True, False), ("0.2", False),
        (-0.1, False), (float("nan"), False),
    ],
)
def test_confidence_threshold_and_invalid_values(confidence, marked):
    alternative = {
        "transcript": "Слово.",
        "words": [{"word": "слово", "punctuated_word": "Слово.", "confidence": confidence}],
    }
    expected = f"{HEADER}\nСлово[?]." if marked else "Слово."
    assert media._format_transcript(alternative) == expected


def test_missing_confidence_is_ignored_even_when_another_word_is_uncertain():
    alternative = {
        "transcript": "Это слово.",
        "words": [
            {"word": "это", "punctuated_word": "Это"},
            {"word": "слово", "punctuated_word": "слово.", "confidence": 0.2},
        ],
    }
    assert media._format_transcript(alternative) == f"{HEADER}\nЭто слово[?]."


def test_empty_transcript_is_unchanged():
    assert media._format_transcript({
        "transcript": "", "words": [{"word": "слово", "confidence": 0.1}],
    }) == ""


def test_markers_preserve_formatted_tokens_whitespace_and_quotes():
    alternative = {
        "transcript": "  «Привет»,\n\t42!  ",
        "words": [
            {"word": "привет", "punctuated_word": "«Привет»,", "confidence": 0.3},
            {"word": "сорок два", "punctuated_word": "42!", "confidence": 0.4},
        ],
    }
    assert media._format_transcript(alternative) == f"{HEADER}\n  «Привет[?]»,\n\t42[?]!  "


def test_repeated_tokens_are_matched_in_order_at_word_boundaries():
    alternative = {
        "transcript": "тесто тест тест",
        "words": [{"word": "тест", "confidence": 0.2}, {"word": "тест", "confidence": 0.9}],
    }
    assert media._format_transcript(alternative) == f"{HEADER}\nтесто тест[?] тест"


def test_smart_format_fallback_retains_all_words_and_markers():
    alternative = {
        "transcript": "Итого 25 рублей.",
        "words": [
            {"word": "итого", "punctuated_word": "Итого", "confidence": 0.9},
            {"word": "двадцать", "confidence": 0.2},
            {"word": "пять", "punctuated_word": "", "confidence": 0.9},
            {"word": "рублей", "punctuated_word": "рублей.", "confidence": 0.3},
        ],
    }
    assert media._format_transcript(alternative) == f"{HEADER}\nИтого двадцать[?] пять рублей[?]."


@pytest.fixture
def deepgram_mock(monkeypatch, tmp_path):
    response = MagicMock()
    response.read = AsyncMock()
    response_context = MagicMock()
    response_context.__aenter__ = AsyncMock(return_value=response)
    http = MagicMock()
    http.post.return_value = response_context
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=http)
    session_factory = MagicMock(return_value=session_context)
    monkeypatch.setattr(aiohttp, "ClientSession", session_factory)
    monkeypatch.setattr(media, "_transcription_cache", {})
    monkeypatch.setattr(media, "TRANSCRIPTION_CACHE_FILE", tmp_path / "transcription_cache.json")
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"test audio")
    return response, http, session_factory, audio


@pytest.mark.asyncio
async def test_transcribe_returns_and_persists_marked_text(deepgram_mock, monkeypatch, caplog):
    response, http, session_factory, audio = deepgram_mock
    response.read.return_value = json.dumps({
        "results": {"channels": [{"alternatives": [{
            "transcript": "Привет, мир!",
            "words": [
                {"word": "привет", "punctuated_word": "Привет,", "confidence": 0.4},
                {"word": "мир", "punctuated_word": "мир!", "confidence": 0.99},
            ],
        }]}]},
        "metadata": {"duration": 60},
    }).encode()
    expected = f"{HEADER}\nПривет[?], мир!"

    assert await media.transcribe(str(audio), "voice-test") == (expected, None)
    assert media._transcription_cache == {"voice-test": expected}
    saved = media._load_transcription_cache()
    assert saved == {"voice-test": expected}
    assert f"Deepgram: 60.0s, $0.0043, {len(expected)} chars" in caplog.text
    http.post.assert_called_once()
    assert http.post.call_args.args == (
        "https://api.deepgram.com/v1/listen?model=nova-3&language=ru&smart_format=true",
    )
    assert http.post.call_args.kwargs["data"] == b"test audio"

    # Simulate loading the on-disk cache after restart; no audio/API is needed.
    monkeypatch.setattr(media, "_transcription_cache", saved)
    audio.unlink()
    assert await media.transcribe(str(audio), "voice-test") == (expected, None)
    session_factory.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_cache_remains_readable(deepgram_mock, monkeypatch):
    _, _, session_factory, audio = deepgram_mock
    media.TRANSCRIPTION_CACHE_FILE.write_text(json.dumps({"old-voice": "Прежний текст."}))
    monkeypatch.setattr(media, "_transcription_cache", media._load_transcription_cache())
    assert await media.transcribe(str(audio), "old-voice") == ("Прежний текст.", None)
    session_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b'{"error": "unavailable"}', b'{"err_msg": "unavailable"}', b"invalid JSON", b"{}"])
async def test_deepgram_errors_do_not_populate_cache(deepgram_mock, body):
    response, _, _, audio = deepgram_mock
    response.read.return_value = body
    text, error = await media.transcribe(str(audio), "failed-voice")
    assert text == ""
    assert error
    assert media._transcription_cache == {}
    assert not media.TRANSCRIPTION_CACHE_FILE.exists()
