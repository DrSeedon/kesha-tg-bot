"""Per-response cost accounting: what the bill must never get wrong.

Money accounting is core: a wrong row here is invisible until someone prices a
subscription from it. Both defects tested below were live in production for one
deploy and were caught only by comparing a row with the CLI transcript.
"""

import sqlite3
import json

import pytest

from message_log import MessageLog


@pytest.fixture
def db(tmp_path):
    return MessageLog(tmp_path / "messages.db")


def _session():
    from claude_session import ClaudeSession

    session = ClaudeSession.__new__(ClaudeSession)
    session.last_response_usage = {}
    session._session_cost_seen = 0.0
    session._session_model_usage_seen = {}
    session.last_cost_usd = None
    session.total_cost_usd = 0.0
    return session


def model_usage(*, out, inp=2, cread=0, ccreate=0, helper_out=0):
    """Shape the SDK reports: per-model, and a RUNNING TOTAL of the session."""
    account = {"claude-opus-5": {
        "inputTokens": inp, "outputTokens": out,
        "cacheReadInputTokens": cread, "cacheCreationInputTokens": ccreate,
    }}
    if helper_out:
        account["claude-haiku-4-5"] = {
            "inputTokens": 0, "outputTokens": helper_out,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        }
    return account


def test_row_survives_a_new_process(db, tmp_path):
    """In-memory counters reset on restart; the row must not."""
    db.log_response_usage(
        720740564, "claude", model="claude-opus-5", num_turns=4, duration_ms=12345,
        input_tokens=7, cache_creation_tokens=9000, cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=9000, cache_read_tokens=500000,
        output_tokens=1200, context_tokens=509007, total_cost_usd=0.42,
    )
    reopened = sqlite3.connect(str(tmp_path / "messages.db"))
    reopened.row_factory = sqlite3.Row
    row = reopened.execute("SELECT * FROM response_usage").fetchone()
    assert (row["chat_id"], row["runtime"], row["total_cost_usd"]) == (
        720740564, "claude", 0.42,
    )
    assert row["cache_creation_1h_tokens"] == 9000
    assert row["context_tokens"] == 509007


def test_unknown_numbers_stay_null_not_zero(db):
    """Codex reports no cost: NULL means 'unknown', 0 would mean 'free'."""
    db.log_response_usage(892, "codex", model="gpt-5.6-sol", input_tokens=100)
    row = db.conn.execute("SELECT * FROM response_usage").fetchone()
    assert row["total_cost_usd"] is None
    assert row["cache_creation_tokens"] is None
    assert row["output_tokens"] is None


def test_tokens_come_from_the_complete_account_as_deltas():
    """`model_usage` covers helper models and sub-agents, and accumulates."""
    session = _session()
    # First answer of the session.
    session._absorb_result_model_usage(
        model_usage(out=242, cread=10447, ccreate=10305, helper_out=17)
    )
    session._absorb_result_usage(
        {"cache_creation": {"ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 10305}},
        tokens=False,
    )
    assert session.last_response_usage["output_tokens"] == 259

    # Second answer: the SDK reports the session total, the answer is the delta.
    session.reset_response_usage()
    session._absorb_result_model_usage(
        model_usage(out=300, inp=4, cread=31191, ccreate=10505, helper_out=17)
    )
    session._absorb_result_usage(
        {"cache_creation": {"ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 200}},
        tokens=False,
    )

    usage = session.last_response_usage
    assert usage["output_tokens"] == 58
    assert usage["cache_read_tokens"] == 20744
    assert usage["cache_creation_tokens"] == 200
    assert usage["cache_creation_1h_tokens"] == 200
    # The result's input side is a SUM over calls, so it must not be read as a
    # context size — that column comes from the per-call snapshots instead.
    assert "context_tokens" not in usage


def test_context_is_the_last_call_not_the_sum():
    """A 3-call answer once recorded 1 459 171 against a real context of 730 857."""
    session = _session()
    session._absorb_assistant_context(
        {"input_tokens": 2, "cache_read_input_tokens": 718442,
         "cache_creation_input_tokens": 1976}
    )
    session._absorb_assistant_context(
        {"input_tokens": 2, "cache_read_input_tokens": 728411,
         "cache_creation_input_tokens": 2444}
    )
    session._absorb_result_model_usage(
        model_usage(out=1910, inp=4, cread=1446853, ccreate=4420)
    )
    assert session.last_response_usage["context_tokens"] == 730857
    assert session.last_response_usage["cache_read_tokens"] == 1446853


def test_cost_is_the_delta_of_a_running_session_total():
    """`total_cost_usd` grows with the session; billing the raw value triples it.

    Production rows before the fix: 5.17, 6.26, 7.35 … 47.94 — each answer was
    charged the whole session's spend to date.
    """
    session = _session()
    result = session._absorb_result_cost

    result(0.0155)
    assert session.last_response_usage["cost_usd"] == pytest.approx(0.0155)

    session.reset_response_usage()
    result(0.0279)
    assert session.last_response_usage["cost_usd"] == pytest.approx(0.0124)

    # A fresh CLI session restarts the counter: the value IS the answer's price.
    session.reset_response_usage()
    result(0.004)
    assert session.last_response_usage["cost_usd"] == pytest.approx(0.004)
    assert session.total_cost_usd == pytest.approx(0.0319)


@pytest.mark.asyncio
async def test_resumed_session_cost_and_tokens_start_at_transcript_baseline(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    from claude_session import ClaudeSession

    session_id = "resumed-session"
    cwd = tmp_path / "runtime"
    cwd.mkdir()
    session_file = tmp_path / "session-id"
    session_file.write_text(session_id)
    transcript = (
        tmp_path
        / "claude-config"
        / "projects"
        / str(cwd).replace("/", "-")
        / f"{session_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "type": "cost-state",
        "sessionId": session_id,
        "totalCostUSD": 52.91,
        "modelUsage": {
            "claude-opus-5-5[1m]": {
                "inputTokens": 300,
                "outputTokens": 1000,
                "cacheReadInputTokens": 10_000,
                "cacheCreationInputTokens": 500,
            }
        },
    }) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    session = ClaudeSession(cwd=str(cwd), session_file=session_file)

    class ResumedClient:
        async def connect(self):
            return None

        async def query(self, _text):
            return None

        async def receive_messages(self):
            yield AssistantMessage(
                content=[TextBlock("resumed answer")],
                model="claude-opus-5-5",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=10,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                result="resumed answer",
                total_cost_usd=53.01,
                model_usage={
                    session.expected_context_model: {
                        "inputTokens": 320,
                        "outputTokens": 1020,
                        "cacheReadInputTokens": 10_200,
                        "cacheCreationInputTokens": 550,
                        "maxOutputTokens": session.expected_max_output_tokens,
                    }
                },
            )

    monkeypatch.setattr("claude_session.ClaudeSDKClient", lambda **_kwargs: ResumedClient())

    await session._ensure_connected()
    session.reset_response_usage()
    chunks = [chunk async for chunk in session.send_message("next")]

    assert chunks[0] == {"type": "text", "content": "resumed answer"}
    assert session.last_response_usage["cost_usd"] == pytest.approx(0.10)
    assert session.last_response_usage["output_tokens"] == 20
    assert session.last_response_usage["input_tokens"] == 20
    assert session.last_response_usage["cache_read_tokens"] == 200
    assert session.last_response_usage["cache_creation_tokens"] == 50


def test_response_usage_uses_the_model_that_answered(db, monkeypatch):
    from types import SimpleNamespace

    import message_log
    import response_stream

    session = SimpleNamespace(
        model="claude-opus-5-5",
        last_response_model="claude-opus-4-8",
        last_response_usage={"cost_usd": 0.42, "num_turns": 1},
        last_duration_ms=100,
    )
    monkeypatch.setattr(response_stream, "_get_session", lambda _chat_id: session)
    monkeypatch.setattr(response_stream, "_runtime_label", lambda _chat_id: "claude")
    monkeypatch.setattr(message_log, "get_db", lambda: db)

    response_stream._log_response_usage(720740564)

    row = db.conn.execute("SELECT model FROM response_usage").fetchone()
    assert row["model"] == "claude-opus-4-8"


def test_reset_opens_a_new_answer():
    session = _session()
    session._absorb_result_model_usage(model_usage(out=10))
    session.reset_response_usage()
    session._absorb_result_model_usage(model_usage(out=17))
    assert session.last_response_usage["output_tokens"] == 7


def test_a_result_without_model_usage_still_records_tokens():
    """Error terminals carry no per-model account; writing nothing loses the spend."""
    session = _session()
    session._absorb_result_model_usage(None)
    session._absorb_result_usage({"output_tokens": 44, "input_tokens": 2})
    assert session.last_response_usage["output_tokens"] == 44


@pytest.mark.asyncio
async def test_a_real_stream_fills_the_row(tmp_path):
    """The wiring, not the helpers: what one answer leaves behind end to end."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    from test_claude_session_limit import collect, make_session, result

    session, client = make_session(tmp_path)
    terminal = result()
    terminal.total_cost_usd = 0.031
    # The complete account: the answer plus the helper model behind it.
    terminal.model_usage = dict(terminal.model_usage or {})
    terminal.model_usage["claude-opus-5"] = {
        "inputTokens": 4, "outputTokens": 1910,
        "cacheReadInputTokens": 1446853, "cacheCreationInputTokens": 4420,
    }
    terminal.model_usage["claude-haiku-4-5"] = {
        "inputTokens": 901, "outputTokens": 17,
        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
    }
    terminal.usage = {
        "input_tokens": 4,
        "output_tokens": 1910,
        "cache_read_input_tokens": 1446853,
        "cache_creation_input_tokens": 4420,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 4420,
        },
    }
    for event in (
        AssistantMessage(
            content=[TextBlock("первый вызов")], model="claude",
            usage={"input_tokens": 2, "output_tokens": 6,
                   "cache_read_input_tokens": 718442,
                   "cache_creation_input_tokens": 1976},
        ),
        AssistantMessage(
            content=[TextBlock("второй вызов")], model="claude",
            usage={"input_tokens": 2, "output_tokens": 5,
                   "cache_read_input_tokens": 728411,
                   "cache_creation_input_tokens": 2444},
        ),
        terminal,
    ):
        client.events.put_nowait(event)

    session.reset_response_usage()
    await collect(session)

    usage = session.last_response_usage
    # 11 is what the snapshots claimed; 1927 is the answer plus its helper model.
    assert usage["output_tokens"] == 1927
    assert usage["input_tokens"] == 905
    assert usage["cache_read_tokens"] == 1446853
    assert usage["cache_creation_1h_tokens"] == 4420
    assert usage["context_tokens"] == 730857
    assert usage["cost_usd"] == pytest.approx(0.031)
    assert usage["num_turns"] == 1


def test_codex_turn_usage_is_snapshot_not_sum():
    """`last` already holds the turn's running total; adding repeats doubles it."""
    from codex_session import CodexSession

    session = CodexSession.__new__(CodexSession)
    session.last_response_usage = {}
    session._turn_usage = {}
    session.last_usage = None
    session._context_window = 400000
    session._context_tokens = None
    session._last_ctx_usage = None
    session.model = "gpt-5.6-sol"

    session._absorb_usage({"last": {"inputTokens": 1000, "outputTokens": 50}})
    session._absorb_usage({"last": {"inputTokens": 1000, "outputTokens": 120}})
    session._fold_turn_usage()

    assert session.last_response_usage["output_tokens"] == 120
    assert session.last_response_usage["input_tokens"] == 1000
    assert session.last_response_usage["context_tokens"] == 1000
    assert session.last_response_usage["num_turns"] == 1
