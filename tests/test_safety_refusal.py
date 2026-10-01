"""A turn refused by Claude's safeguards is cut out of the session.

Stream shape observed in production 01.10.2026 (CLI 2.1.280, refusal fallback
disabled): SystemMessage `model_refusal_no_fallback` naming the refused user
message, a synthetic AssistantMessage with `error="invalid_request"` and the
"API Error: ... safeguards flagged this session" text, then a ResultMessage
with stop_reason="refusal".
"""
import asyncio
import json
import re
import uuid

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from tests.test_claude_session_limit import collect, make_session

FLAGGED = "API Error: Opus 5.5 (1M context)'s safeguards flagged this session"


def write_transcript(config_dir, cwd, session_id, entries):
    project = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
    path = config_dir / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    parent = None
    for kind, text, entry_uuid in entries:
        lines.append(json.dumps({
            "type": kind,
            "uuid": entry_uuid,
            "parentUuid": parent,
            "sessionId": session_id,
            "isSidechain": False,
            "timestamp": "2026-10-01T04:43:12.000Z",
            "cwd": str(cwd),
            "message": {"role": kind, "content": text},
        }))
        parent = entry_uuid
    path.write_text("\n".join(lines) + "\n")
    return path


def refused_session(tmp_path, monkeypatch, entries):
    config_dir = tmp_path / "claude"
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    session_id = str(uuid.uuid4())
    source = write_transcript(config_dir, cwd, session_id, entries)
    session, client = make_session(tmp_path)
    session.cwd = str(cwd)
    session.session_id = session_id
    return session, client, source, config_dir


async def refuse(client, session, refused_uuid, *, stop_reason="refusal"):
    await client.events.put(SystemMessage(
        subtype="model_refusal_no_fallback",
        data={
            "type": "system",
            "subtype": "model_refusal_no_fallback",
            "refusedUserMessageUuid": refused_uuid,
            "apiRefusalCategory": "cyber",
        },
    ))
    await client.events.put(AssistantMessage(
        content=[TextBlock(FLAGGED)], model="<synthetic>", error="invalid_request",
    ))
    await client.events.put(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=2, session_id=session.session_id, result=FLAGGED, stop_reason=stop_reason,
    ))


@pytest.mark.asyncio
async def test_refused_turn_is_cut_and_next_turn_resumes_the_fork(tmp_path, monkeypatch):
    kept_user, kept_answer, refused = (str(uuid.uuid4()) for _ in range(3))
    session, client, source, config_dir = refused_session(tmp_path, monkeypatch, [
        ("user", "earlier question", kept_user),
        ("assistant", "earlier answer", kept_answer),
        ("user", "flagged request", refused),
    ])
    original_id = session.session_id
    source_bytes = source.read_bytes()

    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await refuse(client, session, refused)
    chunks = await task

    refusals = [c for c in chunks if c.get("kind") == "safety_refusal"]
    assert refusals == [{
        "type": "error", "kind": "safety_refusal", "content": "cyber", "rolled_back": True,
    }]
    assert not any(FLAGGED in str(c.get("content")) for c in chunks if c["type"] != "error")
    assert session.session_id not in (None, original_id)
    assert (tmp_path / "session").read_text() == session.session_id
    assert source.read_bytes() == source_bytes

    fork = source.with_name(f"{session.session_id}.jsonl").read_text()
    assert "earlier answer" in fork
    assert "flagged request" not in fork
    # The next connect resumes the fork, not the refused session.
    assert session._make_options().resume == session.session_id


@pytest.mark.asyncio
async def test_refused_opening_message_starts_a_new_session(tmp_path, monkeypatch):
    refused = str(uuid.uuid4())
    session, client, _source, _ = refused_session(
        tmp_path, monkeypatch, [("user", "flagged request", refused)],
    )

    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await refuse(client, session, refused)
    chunks = await task

    assert any(c.get("rolled_back") is True for c in chunks)
    assert session.session_id is None
    assert (tmp_path / "session").read_text() == ""


@pytest.mark.asyncio
async def test_refusal_inside_a_completed_turn_keeps_the_answer(tmp_path, monkeypatch):
    kept_user, refused = str(uuid.uuid4()), str(uuid.uuid4())
    session, client, _source, _ = refused_session(tmp_path, monkeypatch, [
        ("user", "save everything", kept_user),
        ("user", "tool result", refused),
    ])
    original_id = session.session_id

    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(SystemMessage(
        subtype="model_refusal_no_fallback",
        data={"refusedUserMessageUuid": refused, "apiRefusalCategory": "cyber"},
    ))
    await client.events.put(AssistantMessage(
        content=[TextBlock(FLAGGED)], model="<synthetic>", error="invalid_request",
    ))
    await client.events.put(AssistantMessage(
        content=[TextBlock("Saved.")], model=session.expected_context_model,
    ))
    await client.events.put(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=7, session_id=original_id, result="Saved.", stop_reason="end_turn",
    ))
    chunks = await task

    texts = [c["content"] for c in chunks if c["type"] == "text"]
    assert texts == ["Saved."]
    assert not any(c.get("kind") == "safety_refusal" for c in chunks)
    assert session.session_id == original_id
