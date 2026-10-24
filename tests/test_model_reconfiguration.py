"""Changing the configured model must preserve resumed state and compaction."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from claude_session import ClaudeSession, expected_max_output_tokens
from compact import compact_session
from tests.sdk_echo import echo, prompt_frames
from tests.test_claude_session_limit import context_usage, make_session
from tests.test_compact_limit import ScriptedSession, VALID_SUMMARY


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5",
                                  "claude-sonnet-5-5[1m]"])
def test_current_models_have_128k_output(model):
    assert expected_max_output_tokens(model) == 128_000
    assert expected_max_output_tokens("claude-sonnet-4-6") == 64_000


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-sonnet-5-5[1m]"])
async def test_opus_transcript_resumes_on_sonnet_with_usage_and_reserve(tmp_path, monkeypatch, model):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    path = tmp_path / "session"
    path.write_text("synthetic-session")
    session = ClaudeSession(cwd=str(tmp_path), model=model, session_file=path)
    old_usage = {"inputTokens": 300, "outputTokens": 1000, "cacheReadInputTokens": 2000}
    transcript = session._transcript_path(session.session_id)
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "type": "cost-state", "sessionId": session.session_id,
        "totalCostUSD": 2.0, "modelUsage": {"claude-opus-5-5[1m]": old_usage},
    }) + "\n")
    options_seen = []

    class Client:
        def __init__(self, options):
            options_seen.append(options)
            self.events = asyncio.Queue()

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def get_context_usage(self):
            return context_usage(10_000, model="claude-sonnet-5-5")

        async def query(self, prompt):
            for event in echo(await prompt_frames(prompt)):
                self.events.put_nowait(event)
            self.events.put_nowait(AssistantMessage(
                content=[TextBlock("synthetic answer")], model="claude-sonnet-5-5",
            ))
            self.events.put_nowait(ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="synthetic-session",
                result="synthetic answer", total_cost_usd=2.1,
                model_usage={
                    "claude-opus-5-5[1m]": old_usage,
                    "claude-sonnet-5-5[1m]": {
                        "inputTokens": 20, "outputTokens": 30,
                        "cacheReadInputTokens": 200,
                        "maxOutputTokens": 128_000, "contextWindow": 1_000_000,
                        "canonicalModel": "claude-sonnet-5-5", "costBasis": "unknown",
                        "costUSD": 0.1,
                    },
                },
            ))

        async def receive_messages(self):
            while True:
                yield await self.events.get()

    monkeypatch.setattr("claude_session.ClaudeSDKClient", Client)
    await session._ensure_connected()
    assert options_seen[0].model == "claude-sonnet-5-5[1m]"
    assert options_seen[0].resume == "synthetic-session"
    assert session.expected_context_model == options_seen[0].model
    assert (await session.check_context_reserve("next"))["ok"]
    session.reset_response_usage()
    chunks = [chunk async for chunk in session.send_message("next")]
    assert {"type": "text", "content": "synthetic answer"} in chunks
    assert session.session_id == path.read_text() == "synthetic-session"
    assert session.last_response_model == "claude-sonnet-5-5"
    assert session._max_output_tokens_valid
    assert session.last_response_usage["input_tokens"] == 20
    assert session.last_response_usage["output_tokens"] == 30
    assert session.last_response_usage["cache_read_tokens"] == 200
    # Unknown tariff metadata is not a new price table: retain the CLI delta.
    assert session.last_response_usage["cost_usd"] == pytest.approx(0.1)
    assert (await session.check_context_reserve("compact", manual=True))["ok"]
    await session.safe_disconnect()


@pytest.mark.asyncio
async def test_sonnet_preventive_compact_boundary_is_derived_from_active_session(tmp_path):
    session, client = make_session(tmp_path)
    session.model = "claude-sonnet-5-5"
    client.context_usage = context_usage(879_995, model="claude-sonnet-5-5[1m]")
    result = await session.check_context_reserve("next")
    assert result["ok"] is True
    assert result["should_compact"] is False
    client.context_usage = context_usage(880_000, model="claude-sonnet-5-5[1m]")
    result = await session.check_context_reserve("next")
    assert result["ok"] is True
    assert result["should_compact"] is True


@pytest.mark.asyncio
async def test_compact_keeps_sonnet_for_summary_and_candidate(tmp_path):
    path = tmp_path / "session"
    path.write_text("synthetic-source")
    options_seen = []

    async def summary(session):
        options_seen.append(session._make_options())
        yield {"type": "text", "content": VALID_SUMMARY}

    async def candidate(session):
        options_seen.append(session._make_options())
        session.session_id = "synthetic-candidate"
        yield {"type": "text", "content": "ok"}

    session = ScriptedSession(path, [summary, candidate])
    session.model = "claude-sonnet-5-5[1m]"
    result = await compact_session(session)
    assert result["ok"]
    assert [options.model for options in options_seen] == ["claude-sonnet-5-5[1m]"] * 2
    assert options_seen[0].resume == "synthetic-source"
    assert options_seen[1].resume is None
    assert path.read_text() == "synthetic-candidate"


@pytest.mark.asyncio
async def test_tool_and_command_restart_the_same_service(monkeypatch):
    import handlers
    import kesha_tools
    from config import BOT_SERVICE_UNIT

    calls = []

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")))

    monkeypatch.setattr(handlers.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(handlers, "ALLOWED", {1})
    monkeypatch.setattr(handlers, "_send_safe", AsyncMock())
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1, language_code="en"))
    await handlers.h_restart(msg)
    await kesha_tools._do_restart()
    assert calls == [("sudo", "systemctl", "restart", BOT_SERVICE_UNIT)] * 2
    assert BOT_SERVICE_UNIT == "kesha-bot-vps"
