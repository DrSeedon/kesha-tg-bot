"""Context compaction — summarize conversation, reset session, continue with summary."""

import logging
from typing import Optional

logger = logging.getLogger("kesha.compact")


COMPACT_PROMPT = """[SYSTEM: Context compaction requested]

Summarize our conversation so far so it can continue after a context reset. Output in this exact structure (plain text, no markdown headers, ~800 tokens max):

INTENT: What the user is working on right now (1-2 sentences).

DECISIONS: Key technical/design decisions we made (bullet points).

FILES: Files touched in this session with a brief note what was done (path — purpose).

PENDING: Open questions, TODOs, next steps (bullet points).

RECENT: Verbatim copy of the last 3-5 user messages and your replies for continuity.

Do NOT answer the user. Do NOT be creative. Output ONLY the summary block. This will be injected into a fresh session as the starting context."""


CONTINUATION_PREAMBLE = """[PREVIOUS CONTEXT SUMMARY — context was compacted to save tokens]

{summary}

[END OF SUMMARY — continue the conversation naturally below]

"""


async def compact_session(claude, notify=None) -> dict:
    """Summarize current session, reset, continue with summary.

    Args:
        claude: ClaudeSession instance
        notify: optional async callable(text: str) to report progress to user

    Returns:
        dict with keys: ok (bool), before_pct, after_pct, summary_chars, error (optional)
    """
    before = await claude.get_context_usage()
    before_pct = before.get("percentage", 0) if before else 0

    if notify:
        try:
            await notify(f"🗜 Сжимаю контекст... (было {before_pct:.0f}%)")
        except Exception:
            pass

    logger.info(f"Compact: requesting summary, before={before_pct:.1f}%")

    # 1. Ask Claude to summarize — collect only text, ignore tools/streaming bits
    summary_parts: list[str] = []
    try:
        async for chunk in claude.send_message(COMPACT_PROMPT):
            if chunk.get("type") == "text":
                summary_parts.append(chunk["content"])
            # Ignore text_delta during summary — we only need final text blocks
            elif chunk.get("type") == "error":
                raise RuntimeError(f"SDK error during summary: {chunk.get('content')}")
    except Exception as e:
        logger.error(f"Compact: summary request failed: {e}", exc_info=True)
        if notify:
            try:
                await notify(f"⚠️ Сжатие не удалось: {e}")
            except Exception:
                pass
        return {"ok": False, "before_pct": before_pct, "after_pct": before_pct, "summary_chars": 0, "error": str(e)}

    summary = "".join(summary_parts).strip()
    if not summary:
        logger.warning("Compact: Claude returned empty summary, aborting")
        if notify:
            try:
                await notify("⚠️ Кеша вернул пустое саммари, пропускаю сжатие")
            except Exception:
                pass
        return {"ok": False, "before_pct": before_pct, "after_pct": before_pct, "summary_chars": 0, "error": "empty summary"}

    logger.info(f"Compact: got summary {len(summary)} chars, resetting session")

    # 2. Reset session → new session_id. Await disconnect so the next send_message
    #    doesn't race the old client's shutdown (causes 'NoneType has no write' errors).
    await claude.reset_async()

    # 3. Start fresh with summary as opening message. We use send_message so SDK
    #    connects with new session_id and summary becomes the conversation foundation.
    preamble = CONTINUATION_PREAMBLE.format(summary=summary)
    primer_chunks: list[str] = []
    try:
        async for chunk in claude.send_message(preamble + "Ack the summary briefly (one short line)."):
            if chunk.get("type") == "text":
                primer_chunks.append(chunk["content"])
            elif chunk.get("type") == "error":
                # Session will still work, just log — don't fail compact
                logger.warning(f"Compact primer chunk error: {chunk.get('content')}")
    except Exception as e:
        logger.error(f"Compact primer failed: {e}", exc_info=True)
        # Continue anyway — new session exists, summary was set, primer is best-effort

    # 4. Check context usage after
    after = await claude.get_context_usage()
    after_pct = after.get("percentage", 0) if after else 0

    logger.info(f"Compact: done, {before_pct:.1f}% → {after_pct:.1f}%, summary={len(summary)} chars")

    if notify:
        try:
            await notify(f"✅ Контекст сжат: {before_pct:.0f}% → {after_pct:.0f}%")
        except Exception:
            pass

    return {
        "ok": True,
        "before_pct": before_pct,
        "after_pct": after_pct,
        "summary_chars": len(summary),
    }


async def maybe_auto_compact(claude, threshold_pct: float, notify=None) -> Optional[dict]:
    """Check context usage and trigger compact if above threshold. Returns result dict or None."""
    if threshold_pct <= 0 or threshold_pct >= 100:
        return None  # disabled
    usage = await claude.get_context_usage()
    if not usage:
        return None
    pct = usage.get("percentage", 0)
    if pct < threshold_pct:
        return None
    logger.info(f"Auto-compact triggered: {pct:.1f}% >= {threshold_pct}%")
    return await compact_session(claude, notify=notify)
