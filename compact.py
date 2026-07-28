"""Context compaction — summarize conversation, reset session, continue with summary."""

import asyncio
import logging
from typing import Optional

from claude_session import usage_limit_reset

logger = logging.getLogger("kesha.compact")


COMPACT_PROMPT = """[SYSTEM: Context compaction requested — handoff summary]

BEFORE writing the summary — persist your knowledge to files so it survives compact:
1. CLAUDE.md — update with key decisions, new rules, user preferences, patterns discovered this session
2. Create/update relevant .md files in your knowledge base for important topics discussed
3. If there were TODOs or action items — save them to a file so they're not lost
Use your file tools (Edit/Write) NOW to save this information. Then write the summary below.

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
    """Summarize the active session and atomically replace it with the summary."""
    before = await claude.get_context_usage()
    before_pct = before.get("percentage", 0) if before else 0

    async def report(text: str, *, replace: bool) -> None:
        if not notify:
            return
        try:
            await notify(text, replace=replace)
        except Exception as exc:
            logger.warning(f"Compact notification failed: {exc}")

    async def terminal_report(text: str) -> None:
        await asyncio.shield(report(text, replace=True))

    await report(f"🗜 Сжимаю контекст... (было {before_pct:.0f}%)", replace=True)
    logger.info(f"Compact: requesting summary, before={before_pct:.1f}%")

    summary_parts: list[str] = []
    has_deltas = False
    limit_hit = False
    summary_failed = False
    try:
        async for chunk in claude.send_message(COMPACT_PROMPT):
            chunk_type = chunk.get("type")
            content = str(chunk.get("content") or "")
            if chunk.get("kind") == "usage_limit" or usage_limit_reset(content) is not None:
                limit_hit = True
                summary_parts.clear()
                continue
            if limit_hit:
                continue
            if chunk_type == "text_delta":
                has_deltas = True
                summary_parts.append(content)
            elif chunk_type == "text" and not has_deltas:
                summary_parts.append(content)
            elif chunk_type == "error":
                summary_failed = True
    except asyncio.CancelledError:
        await terminal_report("⚠️ Сжатие отменено — контекст сохранён.")
        raise
    except Exception as exc:
        logger.error(f"Compact: summary request failed: {exc}", exc_info=True)
        summary_failed = True

    if limit_hit:
        logger.warning("Compact: usage limit during summary; session replacement skipped")
        await report(
            "⏳ Лимит Claude исчерпан — сжатие пропущено, контекст сохранён.",
            replace=True,
        )
        return {
            "ok": False,
            "reason": "usage_limit",
            "before_pct": before_pct,
            "after_pct": before_pct,
            "summary_chars": 0,
        }

    if summary_failed:
        await report("⚠️ Сжатие не удалось — контекст сохранён.", replace=True)
        return {
            "ok": False,
            "reason": "summary_error",
            "before_pct": before_pct,
            "after_pct": before_pct,
            "summary_chars": 0,
        }

    summary = "".join(summary_parts).strip()
    if not summary:
        logger.warning("Compact: Claude returned empty summary, aborting")
        await report("⚠️ Пустое саммари — сжатие пропущено, контекст сохранён.", replace=True)
        return {
            "ok": False,
            "reason": "empty_summary",
            "before_pct": before_pct,
            "after_pct": before_pct,
            "summary_chars": 0,
        }

    logger.info(f"Compact: got summary {len(summary)} chars, starting replacement")
    logger.debug(f"Compact summary:\n{summary}")
    transaction = claude.begin_session_replacement()
    failure_reason = "preamble_error"

    try:
        preamble = CONTINUATION_PREAMBLE.format(summary=summary)
        preamble_failed = False
        preamble_limit = False
        async for chunk in claude.send_message(preamble):
            if chunk.get("type") != "error":
                continue
            content = str(chunk.get("content") or "")
            if chunk.get("kind") == "usage_limit" or usage_limit_reset(content) is not None:
                preamble_limit = True
                failure_reason = "usage_limit"
            else:
                preamble_failed = True

        if preamble_limit or preamble_failed:
            raise RuntimeError(failure_reason)
        if not claude.session_id:
            failure_reason = "missing_candidate_session"
            raise RuntimeError(failure_reason)

        claude.commit_session_replacement(transaction)
        logger.info(f"Compact: committed session_id={claude.session_id[:8]}...")

        if notify:
            from telegram_io import split_msg

            for part in split_msg(f"📋 Compact summary:\n\n{summary}"):
                await report(part, replace=False)

        try:
            after = await claude.get_context_usage()
            after_pct = after.get("percentage", 0) if after else 0
        except Exception as exc:
            logger.warning(f"Compact: post-commit context usage failed: {exc}")
            after_pct = 0

        await report(
            f"✅ Контекст сжат: {before_pct:.0f}% → {after_pct:.0f}%",
            replace=True,
        )
        logger.info(
            f"Compact: done, {before_pct:.1f}% → {after_pct:.1f}%, "
            f"summary={len(summary)} chars"
        )
        return {
            "ok": True,
            "before_pct": before_pct,
            "after_pct": after_pct,
            "summary_chars": len(summary),
        }
    except asyncio.CancelledError:
        if not transaction.committed:
            await asyncio.shield(claude.rollback_session_replacement(transaction))
            terminal = "⚠️ Сжатие отменено — контекст сохранён."
        else:
            terminal = "✅ Контекст сжат."
        await terminal_report(terminal)
        raise
    except BaseException as exc:
        if not transaction.committed:
            await asyncio.shield(claude.rollback_session_replacement(transaction))
        if not isinstance(exc, Exception):
            raise
        logger.error(f"Compact: candidate session failed: {exc}", exc_info=True)
        if failure_reason == "usage_limit":
            terminal = "⏳ Лимит Claude исчерпан — сжатие пропущено, контекст сохранён."
        else:
            terminal = "⚠️ Сжатие не удалось — контекст сохранён."
        await report(terminal, replace=True)
        return {
            "ok": False,
            "reason": failure_reason,
            "before_pct": before_pct,
            "after_pct": before_pct,
            "summary_chars": len(summary),
        }


async def maybe_auto_compact(claude, threshold_pct: float, notify=None) -> Optional[dict]:
    """Check context usage and trigger compact if above threshold. Returns result dict or None."""
    if threshold_pct <= 0 or threshold_pct >= 100:
        return None
    if claude.usage_limit_active:
        logger.info("Auto-compact skipped while usage-limit latch is active")
        return {"ok": False, "reason": "usage_limit", "skipped": True}
    usage = await claude.get_context_usage()
    if not usage:
        return None
    pct = usage.get("percentage", 0)
    if pct < threshold_pct:
        return None
    logger.info(f"Auto-compact triggered: {pct:.1f}% >= {threshold_pct}%")
    return await compact_session(claude, notify=notify)
