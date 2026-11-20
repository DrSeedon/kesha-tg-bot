import sqlite3

import skillcache_trace as trace


def test_no_raw_arguments_or_tool_results_reach_journal(monkeypatch, tmp_path):
    path = tmp_path / "trace.db"
    monkeypatch.setenv("KESHA_SKILLCACHE_TRACE_DB", str(path))
    invocation = trace.begin_turn(123)
    raw = {"query": "sensitive@example.com", "page_token": "private-token", "body": "private body"}
    shape = trace.arg_shape(raw, set(raw))
    ids = trace.begin_event(123, "gmail", "search_gmail_messages", shape, schema_digest="a" * 64)
    assert trace.finish_event(123, *ids, blocks=1, error=False)
    trace.terminal(123, 17, True)
    trace.finish_turn(123, invocation)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT usage_ref, eligible FROM invocation").fetchone() == (17, 1)
        row = conn.execute("SELECT seq, server, tool, args_schema_bound, result_summary FROM event").fetchone()
        assert row[:3] == (1, "gmail", "search_gmail_messages")
        assert "private" not in str(row) and "example.com" not in str(row)
        assert "body" not in row[3] and "query" in row[3]


def test_steer_and_late_results_are_excluded(monkeypatch, tmp_path):
    path = tmp_path / "trace.db"
    monkeypatch.setenv("KESHA_SKILLCACHE_TRACE_DB", str(path))
    old = trace.begin_turn(123)
    ids = trace.begin_event(123, "gmail", "search_gmail_messages", {})
    trace.mark(123, "steered")
    trace.terminal(123, 18, True)
    trace.finish_turn(123, old)
    new = trace.begin_turn(123)
    assert not trace.finish_event(123, *ids, blocks=1, error=False)
    current = trace.begin_event(123, "gmail", "search_gmail_messages", {})
    assert trace.finish_event(123, *current, blocks=2, error=False)
    trace.terminal(123, 19, True)
    trace.finish_turn(123, new)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT usage_ref, eligible, reason FROM invocation ORDER BY usage_ref").fetchall() == [
            (18, 0, "incomplete"), (19, 1, None),
        ]


def test_trace_projection_never_accepts_unbound_arguments():
    assert trace.arg_shape({"folder": "synthetic-folder", "secret": "token"}, {"folder"}) is None



def test_capsule_tool_is_opt_in_and_uses_only_request_argument(monkeypatch):
    import kesha_tools

    monkeypatch.delenv("KESHA_SKILLCACHE_KEY", raising=False)
    assert "capsule_run" not in {tool.name for tool in kesha_tools.bridge_tools()}
    monkeypatch.setenv("KESHA_SKILLCACHE_KEY", "x" * 32)
    selected = next(tool for tool in kesha_tools.bridge_tools() if tool.name == "capsule_run")
    assert kesha_tools.tool_arg_names(selected) == {"request"}
