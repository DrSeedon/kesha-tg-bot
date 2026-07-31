from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from claude_session import ClaudeSession
from compact import (
    COMPACT_PROMPT,
    SUMMARY_SECTIONS,
    _redact_high_confidence_secrets,
    _validate_summary_sections,
    compact_session,
)
from compact_summary_scorer import load_cases, render_case, score_case


def valid_summary(*, recent="- first\n- second\n- third", extra="") -> str:
    blocks = []
    for section in SUMMARY_SECTIONS:
        if section == "RECENT VERBATIM":
            body = recent
        elif section == "CONTINUATION":
            body = "- Continue with the next verified step."
        else:
            body = f"- {section.title()} evidence.{extra}"
        blocks.extend((section, body))
    return "\n".join(blocks)


class ScriptedSession(ClaudeSession):
    def __init__(self, session_file: Path, scripts):
        super().__init__(cwd=".", session_file=session_file)
        self.scripts = list(scripts)
        self.sent = []
        self.usage_calls = 0

    async def get_context_usage(self, **_kwargs):
        self.usage_calls += 1
        return {"percentage": 100 if self.usage_calls == 1 else 4}

    async def send_message(self, text):
        self.sent.append(text)
        script = self.scripts.pop(0)
        if callable(script):
            async for chunk in script(self):
                yield chunk
            return
        for chunk in script:
            yield chunk

class Notices:
    def __init__(self):
        self.items = []

    async def __call__(self, text, *, replace):
        self.items.append((text, replace))


def session_file(tmp_path):
    path = tmp_path / "session"
    path.write_text("sid-old")
    return path


async def candidate(session):
    session.session_id = "sid-candidate"
    if False:
        yield {}


def test_prompt_contains_complete_security_and_preservation_contract():
    assert "GLOBAL SECURITY RULE" in COMPACT_PROMPT
    assert "ANY file or ANY handoff section" in COMPACT_PROMPT
    assert "Make writes idempotent" in COMPACT_PROMPT
    assert "Keep CLAUDE.md for stable operating rules only" in COMPACT_PROMPT
    assert "runtime date are constraints, not conversation facts" in COMPACT_PROMPT
    for section in SUMMARY_SECTIONS:
        assert f"\n## {section}\n" in COMPACT_PROMPT


def test_redactor_covers_high_confidence_secret_shapes():
    raw = """api_key = sk-ant-abcdefghijklmnop
password: "synthetic-password"
github_pat_abcdefghijklmnop
-----BEGIN PRIVATE KEY-----
synthetic
-----END PRIVATE KEY-----"""

    redacted = _redact_high_confidence_secrets(raw)

    assert "synthetic-password" not in redacted
    assert "sk-ant-abcdefghijklmnop" not in redacted
    assert "github_pat_abcdefghijklmnop" not in redacted
    assert "synthetic\n" not in redacted
    assert "[REDACTED SECRET: api key]" in redacted
    assert "[REDACTED SECRET: password]" in redacted
    assert "[REDACTED SECRET: token]" in redacted
    assert "[REDACTED SECRET: private key]" in redacted


def test_validator_ignores_header_like_recent_payload_but_rejects_structure_errors():
    recent = "- exact user text\nOBJECTIVE\nCONTINUATION\n- still exact user text"
    assert _validate_summary_sections(valid_summary(recent=recent))
    markdown = "\n".join(
        f"## {line}" if line in SUMMARY_SECTIONS else line
        for line in valid_summary(recent=recent).splitlines()
    )
    assert _validate_summary_sections(markdown)

    missing = valid_summary().replace("TEMPORAL STATE\n", "", 1)
    assert not _validate_summary_sections(missing)

    out_of_order = valid_summary().replace(
        "DECISIONS\n- Decisions evidence.\nFILES AND ARTIFACTS",
        "FILES AND ARTIFACTS\n- Files And Artifacts evidence.\nDECISIONS",
    )
    assert not _validate_summary_sections(out_of_order)

    duplicate = valid_summary().replace(
        "FILES AND ARTIFACTS",
        "DECISIONS\n- duplicate\nFILES AND ARTIFACTS",
        1,
    )
    assert not _validate_summary_sections(duplicate)


@pytest.mark.asyncio
async def test_secret_is_redacted_before_log_preamble_and_telegram(
    tmp_path, caplog
):
    secret = "sk-ant-syntheticabcdefghijkl"
    summary = valid_summary(recent=f"- token={secret}")
    session = ScriptedSession(
        session_file(tmp_path),
        [[{"type": "text", "content": summary}], candidate],
    )
    notices = Notices()
    caplog.set_level("DEBUG", logger="kesha.compact")

    result = await compact_session(session, notify=notices)

    assert result["ok"] is True
    assert secret not in caplog.text
    assert secret not in session.sent[1]
    assert all(secret not in text for text, _replace in notices.items)
    assert "[REDACTED SECRET: token]" in session.sent[1]


@pytest.mark.asyncio
async def test_malformed_summary_preserves_sid_and_never_starts_candidate(tmp_path):
    path = session_file(tmp_path)
    session = ScriptedSession(
        path,
        [[{"type": "text", "content": "OBJECTIVE\n- incomplete"}]],
    )
    notices = Notices()

    result = await compact_session(session, notify=notices)

    assert result["reason"] == "invalid_summary"
    assert session.session_id == "sid-old"
    assert path.read_text() == "sid-old"
    assert session._session_replacement is None
    assert len(session.sent) == 1
    assert all("📋" not in text for text, _replace in notices.items)


@pytest.mark.asyncio
async def test_overload_is_normalized_for_resumable_live_evaluation(tmp_path):
    session = ScriptedSession(
        session_file(tmp_path),
        [[{
            "type": "error",
            "content": "API Error: 529 Overloaded. This is a server-side issue.",
        }]],
    )

    result = await compact_session(session)

    assert result["reason"] == "transient_overloaded"
    assert session.session_id == "sid-old"


@pytest.mark.asyncio
async def test_context_limit_preserves_original_session_without_retry(tmp_path):
    path = session_file(tmp_path)
    session = ScriptedSession(
        path,
        [[{"type": "error", "kind": "context_limit", "content": "Prompt is too long"}]],
    )

    result = await compact_session(session)

    assert result["reason"] == "context_limit"
    assert len(session.sent) == 1
    assert path.read_text() == "sid-old"


@pytest.mark.asyncio
async def test_preamble_overload_remains_retryable_infrastructure_failure(tmp_path):
    path = session_file(tmp_path)
    session = ScriptedSession(
        path,
        [
            [{"type": "text", "content": valid_summary()}],
            [{
                "type": "error",
                "content": "API Error: 529 Overloaded",
            }],
        ],
    )

    result = await compact_session(session)

    assert result["reason"] == "transient_overloaded"
    assert path.read_text() == "sid-old"


def _fixture_summary(case):
    anchors = "; ".join(case["required_anchors"])
    recent = "\n".join(case["recent_messages"])
    return valid_summary(
        recent=recent,
        extra=f" {anchors}",
    )


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_all_ten_fixture_schemas_pass_deterministic_scorer(case):
    secrets = {
        name: f"synthetic-{name.lower()}-value-not-real"
        for name in case.get("secret_placeholders", [])
    }
    rendered = render_case(case, secrets)
    summary = _fixture_summary(rendered)

    result = score_case(
        rendered,
        summary,
        rendered.get("expected_files", {}),
        list(secrets.values()),
    )

    assert result["passed"], result


def test_scorer_hard_fails_instead_of_averaging_categories():
    case = load_cases()[0]
    summary = _fixture_summary(case).replace(
        case["required_anchors"][0],
        "missing",
    )

    result = score_case(case, summary, {}, [])

    assert result["passed"] is False
    assert result["categories"]["anchors"] is False


def test_source_ledger_ignores_list_ordinals_but_rejects_unsourced_values():
    case = load_cases()[0]
    summary = _fixture_summary(case)

    numbered = summary.replace(
        "- Objective evidence.",
        "4. Objective evidence.",
    )
    assert score_case(case, numbered, {}, [])["categories"]["source_ledger"]

    fabricated = summary.replace(
        "- Objective evidence.",
        "- Objective evidence measured 987654 tokens.",
    )
    assert not score_case(
        case,
        fabricated,
        {},
        [],
    )["categories"]["source_ledger"]
