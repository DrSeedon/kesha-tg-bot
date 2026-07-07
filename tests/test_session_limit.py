"""Session-limit detection — non-retryable vs transient session/process errors.

Bug: Claude CLI 'You've hit your session limit · resets 2:20pm' contains 'session' →
old code reconnect+retried 2-3x, each hit the same limit. Must detect limit → wait, not retry.
"""

import response_stream as rs


def test_detects_session_limit_with_reset():
    r = rs._session_limit_reset("You've hit your session limit · resets 2:20pm (Europe/Berlin)")
    assert r is not None
    assert "2:20pm" in r and "Europe/Berlin" in r


def test_detects_usage_limit():
    assert rs._session_limit_reset("Claude AI usage limit reached. resets 9am") is not None


def test_detects_bare_session_limit_no_reset_time():
    r = rs._session_limit_reset("session limit exceeded")
    assert r == ""  # matched as limit, but no parseable reset → empty string (not None)


def test_transient_session_error_is_not_limit():
    # 'No conversation found' / 'process exited' → transient → reconnect (must return None)
    assert rs._session_limit_reset("No conversation found for session abc") is None
    assert rs._session_limit_reset("process exited with code 1") is None


def test_connection_reset_not_mistaken_for_limit():
    # 'reset' substring must NOT trigger limit detection
    assert rs._session_limit_reset("SDK error: connection reset by peer") is None


def test_empty_and_generic_errors_not_limit():
    assert rs._session_limit_reset("") is None
    assert rs._session_limit_reset("some random error") is None
