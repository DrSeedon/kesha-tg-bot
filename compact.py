"""Context compaction — summarize conversation, reset session, continue with summary."""

import logging
from typing import Optional

logger = logging.getLogger("kesha.compact")


COMPACT_PROMPT = """[SYSTEM: Context compaction requested — handoff summary]

Write a detailed handoff summary so your next session can continue seamlessly. This is the ONLY context your next session will have. Be thorough.

INTENT: What the user is working on and why (2-3 sentences with full context).

DECISIONS: Key decisions made during this session (bullet points, include reasoning).

FILES: Files touched with what was done (path — description of change).

PENDING: Open questions, TODOs, next steps, blockers.

RECENT: Last 5-10 exchanges — what was asked, what you did, what the result was.

BUGS: Bugs found, workarounds applied, things that didn't work.

IMPORTANT CONTEXT: Anything the next session MUST know — user preferences, discovered quirks, traps to avoid, active reminders context.

Output ONLY the summary. Be specific — names, paths, numbers, not vague descriptions."""


CONTINUATION_PREAMBLE = """[PREVIOUS CONTEXT SUMMARY — context was compacted]

{summary}

[END OF SUMMARY — reply with exactly "OK" and nothing else. Wait for the next user message.]

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

    summary_parts: list[str] = []
    has_deltas = False
    try:
        async for chunk in claude.send_message(COMPACT_PROMPT):
            ct = chunk.get("type")
            if ct == "text_delta":
                has_deltas = True
                summary_parts.append(chunk["content"])
            elif ct == "text" and not has_deltas:
                summary_parts.append(chunk["content"])
            elif ct == "error":
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
    logger.debug(f"Compact summary:\n{summary}")

    logger.info(f"Compact: pre-reset session_id={claude.session_id[:8] + '...' if claude.session_id else 'None'}")
    await claude.reset_async()
    logger.info(f"Compact: post-reset session_id={claude.session_id[:8] + '...' if claude.session_id else 'None'}")

    preamble = CONTINUATION_PREAMBLE.format(summary=summary)
    preamble_ok = True
    try:
        async for chunk in claude.send_message(preamble):
            if chunk.get("type") == "error":
                logger.warning(f"Compact preamble error: {chunk.get('content')}")
                preamble_ok = False
    except Exception as e:
        logger.error(f"Compact preamble failed: {e}", exc_info=True)
        preamble_ok = False

    if not claude.session_id:
        logger.error("Compact: no session_id after preamble — session lost")
        preamble_ok = False

    logger.info(f"Compact: preamble done (ok={preamble_ok}), new session_id={claude.session_id[:8] + '...' if claude.session_id else 'None'}")

    after = await claude.get_context_usage()
    after_pct = after.get("percentage", 0) if after else 0

    logger.info(f"Compact: done, {before_pct:.1f}% → {after_pct:.1f}%, summary={len(summary)} chars")

    if preamble_ok:
        if notify:
            try:
                await notify(f"✅ Контекст сжат: {before_pct:.0f}% → {after_pct:.0f}%")
            except Exception:
                pass
    else:
        if notify:
            try:
                await notify(f"⚠️ Контекст сброшен, но саммари могло не загрузиться ({before_pct:.0f}% → {after_pct:.0f}%)")
            except Exception:
                pass

    return {
        "ok": preamble_ok,
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
