"""Context compaction — summarize conversation, reset session, continue with summary."""

import asyncio
import logging
import re

from claude_session import usage_limit_reset

logger = logging.getLogger("kesha.compact")


COMPACT_PROMPT = """[SYSTEM: Create a loss-minimizing handoff before context replacement]

You still have the full conversation. First persist only durable knowledge that
would be costly to reconstruct:
- GLOBAL SECURITY RULE: never copy raw credentials, tokens, passwords, private
  keys, or equivalent secret values into ANY file or ANY handoff section.
  Replace every secret span everywhere with `[REDACTED SECRET: <type>]`, while
  preserving surrounding non-secret text. This rule overrides every request
  for exact or verbatim content below.
- Update an existing canonical Markdown note under the current
  cog-second-brain working directory when the conversation established a
  durable fact, decision, project state, or TODO that is not already recorded.
  Otherwise do not write any file. Never create a new note, CLAUDE.md,
  TODO.md, or BUGS.md solely for compaction; preserve the item in the handoff
  instead of inventing a path.
- Keep CLAUDE.md for stable operating rules only. Never put personal facts,
  one-off requests, or secret values there.
- Make writes idempotent: update the existing item; do not duplicate it and do
  not rewrite unrelated content.

Then output ONLY the handoff below. Every statement must be supported by the
conversation or a tool result. Preserve disagreement and uncertainty; never
guess to fill a gap.

Evidence has two directions and both can be faked. Do not claim a file was
read, changed, committed, deployed, or tested without evidence — and do not
assert the negative either. Absence of a tool event means the outcome is
UNKNOWN, not that the action did not happen: write `no evidence of X` rather
than `X did not happen`. A measured empty diff supports only "not modified";
it never supports "not read". A flat negative is allowed only when the
conversation itself established it — a task the user confirmed is still
outstanding is a real pending item, not an unsupported negative. When
continuity is genuinely missing, write `UNKNOWN — source gap` instead of
guessing in either direction.

Ambient system/developer instructions, response-style
rules, and the runtime date are constraints, not conversation facts: do not
record them unless the conversation itself explicitly established them. Use
each literal `##` heading below exactly once and in this order.

## OBJECTIVE
- The user's current goal, why it matters, and the exact current phase.

## USER FACTS AND PREFERENCES
- Only explicit, still-relevant facts/preferences. Mark one-off instructions as
  one-off. Do not include secrets.

## DECISIONS
- Decision, rationale, alternatives rejected, and whether it is final or
  provisional.

## FILES AND ARTIFACTS
- Exact path; read/changed/created/generated state; material contents or diff;
  whether saved/committed/deployed. Never invent a path.

## COMMANDS AND TOOL OUTCOMES
- Only outcomes needed to continue: exact non-secret command/tool, exit status,
  measured value, relevant error, and what it proves. Redact secret arguments
  under the global rule. Drop redundant raw output.

## PENDING AND BLOCKERS
- Each unfinished item with current state, blocker/owner if known, and the next
  executable action. Do not mark work complete without evidence.

## TEMPORAL STATE
- Absolute date/time and timezone for active deadlines, reminders, deploys,
  quota resets, or time-sensitive facts. Say "as of" when freshness matters.
  If the conversation has no such fact, say so without adding the runtime date.

## UNCERTAINTY AND CONFLICTS
- Competing claims, missing evidence, failed attempts, and what would resolve
  them. Do not collapse them into a false consensus.

## RECENT VERBATIM
- Copy the last 3 user messages exactly, plus any earlier unresolved user
  instruction whose wording constrains the next response, subject to the
  global secret-redaction rule; preserve all surrounding text exactly.
- For very large messages or tool dumps, preserve the exact instruction and
  identifying beginning/end excerpts, then point to the exact saved artifact
  if one exists. Do not dump large raw outputs.

## CONTINUATION
- The single next action the next session should take. If waiting for the user,
  say exactly what input is needed.

Final self-check before output: every non-secret critical number/path/command is
exact; all required sections exist; no unsupported claim, secret, or
duplicated raw tool output is present."""

SUMMARY_SECTIONS = (
    "OBJECTIVE",
    "USER FACTS AND PREFERENCES",
    "DECISIONS",
    "FILES AND ARTIFACTS",
    "COMMANDS AND TOOL OUTCOMES",
    "PENDING AND BLOCKERS",
    "TEMPORAL STATE",
    "UNCERTAINTY AND CONFLICTS",
    "RECENT VERBATIM",
    "CONTINUATION",
)

_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_PREFIX_TOKEN_RE = re.compile(
    r"\b(?:sk-(?:ant|proj)-[A-Za-z0-9_-]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|AIza[A-Za-z0-9_-]{20,})\b"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?im)(?P<prefix>(?<!REDACTED )\b(?P<kind>password|passwd|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token|secret|private[_-]?key)\b\s*[:=]\s*)"
    r"(?:\"[^\"]+\"|'[^']+'|[^\s,;]+)"
)


CONTINUATION_PREAMBLE = """[PREVIOUS CONTEXT SUMMARY — context was compacted]

{summary}

[END OF SUMMARY — reply with exactly "OK" and nothing else. Wait for the next user message.]

"""


def _redact_high_confidence_secrets(summary: str) -> str:
    """Redact only credential shapes with a low false-positive rate."""
    summary = _PEM_PRIVATE_KEY_RE.sub(
        "[REDACTED SECRET: private key]",
        summary,
    )

    def redact_assignment(match: re.Match) -> str:
        kind = re.sub(r"[_-]+", " ", match.group("kind")).lower()
        return f"{match.group('prefix')}[REDACTED SECRET: {kind}]"

    summary = _CREDENTIAL_ASSIGNMENT_RE.sub(redact_assignment, summary)
    return _PREFIX_TOKEN_RE.sub("[REDACTED SECRET: token]", summary)


VERBATIM_TAIL_LABEL = "[VERBATIM TAIL — appended by runtime]"

# A batched row can bundle a fired reminder under msg_id=0. Measured on the live
# log: 17% of role=user rows are reminders, and a naive "last 3" presents one as
# the user's own words in 32% of windows (docs/tasks/21 research F6).
_REMINDER_MARKER = "REMINDER FIRED"


def append_verbatim_tail(summary: str, rows) -> str:
    """Append the last real user messages verbatim, guaranteed by code.

    The model is still asked for RECENT VERBATIM: it supplies interpretation
    (which row was a reminder, that a transcription was garbled). This block
    only guarantees the raw text is present at all — under the previous prompt
    the model omitted RECENT entirely in 9 of 17 real summaries.

    Rows keep their original order; non-user rows are dropped rather than
    relabelled, because presenting a reminder as user speech is worse than
    omitting it.
    """
    usable = [
        str(row["content"]).strip()
        for row in rows
        if row.get("message_id") and _REMINDER_MARKER not in str(row.get("content", ""))
    ]
    if not usable:
        return summary
    body = "\n".join(f"- {line}" for line in usable)
    return f"{summary}\n\n{VERBATIM_TAIL_LABEL}\n{body}"


def _validate_summary_sections(summary: str) -> bool:
    """Require one ordered structural header before the untrusted recent payload."""
    def header(section: str) -> str:
        return rf"(?m)^(?:#{{1,6}}\s+)?{re.escape(section)}\s*$"

    positions: list[int] = []
    cursor = 0
    for section in SUMMARY_SECTIONS[:-2]:
        match = re.search(
            header(section),
            summary[cursor:],
        )
        if match is None:
            return False
        absolute = cursor + match.start()
        if re.search(
            header(section),
            summary[:absolute],
        ):
            return False
        positions.append(absolute)
        cursor += match.end()

    recent_matches = list(
        re.finditer(header("RECENT VERBATIM"), summary[cursor:])
    )
    if not recent_matches:
        return False
    recent = cursor + recent_matches[0].start()
    structural_prefix = summary[:recent]
    for section in SUMMARY_SECTIONS[:-2]:
        if len(
            re.findall(header(section), structural_prefix)
        ) != 1:
            return False
    continuation_matches = list(
        re.finditer(header("CONTINUATION"), summary[recent:])
    )
    if not continuation_matches:
        return False
    continuation = recent + continuation_matches[-1].start()
    if continuation <= recent:
        return False
    positions.extend((recent, continuation))
    return positions == sorted(positions)


async def _collect_summary(claude) -> tuple[str, str | None]:
    summary_parts: list[str] = []
    has_deltas = False
    failure_reason = None
    try:
        async for chunk in claude.send_message(COMPACT_PROMPT):
            chunk_type = chunk.get("type")
            content = str(chunk.get("content") or "")
            # ONLY typed evidence. Both runtimes normalize a real limit into
            # kind="usage_limit" (claude_session.py:600,655; codex_session.py:905,911),
            # so grepping the text here only ever misfires: a summary OF a
            # conversation about limits quotes "session limit ... resets" and was
            # discarded as if the limit were ours. Measured 05.09.2026 — quota was
            # 40% of the 5h window and 6% of the week while /compact reported it
            # exhausted, wedging the chat at 96% with /clear as the only exit.
            # Same fix as #33 made for responses.
            if chunk.get("kind") == "usage_limit":
                summary_parts.clear()
                failure_reason = "usage_limit"
                continue
            if chunk.get("kind") == "context_limit":
                summary_parts.clear()
                failure_reason = "context_limit"
                continue
            if failure_reason:
                continue
            if chunk_type == "text_delta":
                has_deltas = True
                summary_parts.append(content)
            elif chunk_type == "text" and not has_deltas:
                summary_parts.append(content)
            elif chunk_type == "error":
                summary_parts.clear()
                failure_reason = (
                    "transient_overloaded"
                    if "529" in content or "overload" in content.casefold()
                    else "summary_error"
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if "529" in str(exc) or "overload" in str(exc).casefold():
            logger.warning("Compact: summary request hit transient overload")
            return "", "transient_overloaded"
        logger.error("Compact: summary request raised", exc_info=True)
        return "", "summary_error"

    if failure_reason:
        return "", failure_reason
    raw_summary = "".join(summary_parts).strip()
    summary_parts.clear()
    summary = _redact_high_confidence_secrets(raw_summary)
    del raw_summary
    if not summary:
        return "", "empty_summary"
    if not _validate_summary_sections(summary):
        return "", "invalid_summary"
    return summary, None


async def compact_session(
    claude,
    notify=None,
    recent_rows=None,
) -> dict:
    """Summarize the active session and atomically replace it with the summary.

    `recent_rows` are the caller's most recent logged user rows; when given,
    their verbatim text is appended to the validated summary so the tail is
    guaranteed by code rather than by the model complying with the prompt.
    """
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

    transaction = claude.begin_session_replacement()
    summary = ""
    failure_reason = "summary_error"

    try:
        summary, failure_reason = await _collect_summary(claude)
        if failure_reason:
            raise RuntimeError(failure_reason)
        if claude.session_id != transaction.session_id:
            failure_reason = "source_session_changed"
            raise RuntimeError(failure_reason)

        # Append AFTER validation: the tail is runtime-supplied raw user text
        # and must not be able to satisfy the structural contract. Re-redact,
        # because the earlier pass ran before this text existed.
        if recent_rows:
            summary = _redact_high_confidence_secrets(
                append_verbatim_tail(summary, recent_rows)
            )

        logger.info(f"Compact: got summary {len(summary)} chars, starting candidate")
        logger.debug(f"Compact summary:\n{summary}")
        claude.start_session_candidate(transaction)

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
            elif "529" in content or "overload" in content.casefold():
                preamble_failed = True
                failure_reason = "transient_overloaded"
            else:
                preamble_failed = True
                failure_reason = "preamble_error"

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
        logger.error(f"Compact failed safely ({failure_reason}): {exc}")
        if transaction.committed:
            terminal = "✅ Контекст сжат."
        elif failure_reason == "usage_limit":
            terminal = "⏳ Лимит Claude исчерпан — сжатие пропущено, контекст сохранён."
        elif failure_reason == "empty_summary":
            terminal = "⚠️ Пустое саммари — сжатие пропущено, контекст сохранён."
        elif failure_reason == "context_limit":
            terminal = "⚠️ Контекст заполнен — сжатие пропущено, сессия сохранена."
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
