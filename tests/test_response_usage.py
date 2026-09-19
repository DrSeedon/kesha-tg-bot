"""Per-response cost accounting: what the bill must never get wrong.

Money and quota accounting is core: a wrong row here is invisible until someone
prices a subscription from it.
"""

import sqlite3

import pytest

from message_log import MessageLog


@pytest.fixture
def db(tmp_path):
    return MessageLog(tmp_path / "messages.db")


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


def test_repeated_message_id_is_billed_once():
    """The stream repeats one message per content block, with the same usage.

    Summing every repeat inflated a real transcript by 454/260 (19.09.2026).
    """
    from claude_session import ClaudeSession

    session = ClaudeSession.__new__(ClaudeSession)
    session.last_response_usage = {}
    session._counted_message_ids = set()

    usage = {
        "input_tokens": 3,
        "output_tokens": 500,
        "cache_read_input_tokens": 60000,
        "cache_creation_input_tokens": 250,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 250,
        },
    }
    for _ in range(3):
        session._absorb_assistant_usage(usage, "msg_same")
    session._absorb_assistant_usage(
        {"input_tokens": 1, "output_tokens": 40, "cache_read_input_tokens": 61000},
        "msg_next",
    )

    assert session.last_response_usage["output_tokens"] == 540
    assert session.last_response_usage["cache_read_tokens"] == 121000
    assert session.last_response_usage["cache_creation_1h_tokens"] == 250
    # Context = what the LAST call carried, not the sum of all calls.
    assert session.last_response_usage["context_tokens"] == 61001


def test_reset_opens_a_new_answer_but_retries_add_up():
    from claude_session import ClaudeSession

    session = ClaudeSession.__new__(ClaudeSession)
    session.last_response_usage = {}
    session._counted_message_ids = set()

    session._absorb_assistant_usage({"output_tokens": 10}, "msg_a")
    session.reset_response_usage()
    session._absorb_assistant_usage({"output_tokens": 7}, "msg_b")
    # Same id as before the reset: a retry is a real second call, bill it.
    session._absorb_assistant_usage({"output_tokens": 7}, "msg_a")

    assert session.last_response_usage["output_tokens"] == 14


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
