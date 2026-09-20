"""Per-response cost accounting: what the bill must never get wrong.

Money accounting is core: a wrong row here is invisible until someone prices a
subscription from it. Both defects tested below were live in production for one
deploy and were caught only by comparing a row with the CLI transcript.
"""

import sqlite3

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
    session.last_cost_usd = None
    session.total_cost_usd = 0.0
    return session


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


def test_result_usage_is_summed_over_retries():
    """One answer can cost two calls; both were really spent."""
    session = _session()
    session._absorb_result_usage({
        "input_tokens": 2,
        "output_tokens": 242,
        "cache_read_input_tokens": 10447,
        "cache_creation_input_tokens": 10305,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 10305,
        },
    })
    session._absorb_result_usage({
        "input_tokens": 2,
        "output_tokens": 58,
        "cache_read_input_tokens": 20744,
        "cache_creation_input_tokens": 200,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 200,
        },
    })

    usage = session.last_response_usage
    assert usage["output_tokens"] == 300
    assert usage["cache_read_tokens"] == 31191
    assert usage["cache_creation_1h_tokens"] == 10505
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
    session._absorb_result_usage(
        {"input_tokens": 4, "cache_read_input_tokens": 1446853,
         "cache_creation_input_tokens": 4420, "output_tokens": 1910}
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


def test_reset_opens_a_new_answer():
    session = _session()
    session._absorb_result_usage({"output_tokens": 10})
    session.reset_response_usage()
    session._absorb_result_usage({"output_tokens": 7})
    assert session.last_response_usage["output_tokens"] == 7


@pytest.mark.asyncio
async def test_a_real_stream_fills_the_row(tmp_path):
    """The wiring, not the helpers: what one answer leaves behind end to end."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    from test_claude_session_limit import collect, make_session, result

    session, client = make_session(tmp_path)
    terminal = result()
    terminal.total_cost_usd = 0.031
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
    # 11 is what the snapshots claimed; 1910 is what the answer actually produced.
    assert usage["output_tokens"] == 1910
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
