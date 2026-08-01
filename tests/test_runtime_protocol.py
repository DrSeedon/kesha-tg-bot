"""T1 — the runtime contract must match what callers actually use."""

import inspect

import pytest

from claude_session import ClaudeSession
from runtime_protocol import ChatRuntime, RuntimeCapabilities


def test_claude_session_satisfies_protocol(tmp_path):
    """session_id/model are instance attributes — check a constructed session, not the class."""
    session = ClaudeSession(cwd=str(tmp_path), session_file=tmp_path / "sess")
    assert isinstance(session, ChatRuntime)


@pytest.mark.parametrize(
    "name",
    [
        "send_message",
        "check_context_reserve",
        "get_context_usage",
        "interrupt",
        "reconnect",
        "reset_async",
        "safe_disconnect",
    ],
)
def test_contract_method_exists(name):
    assert callable(getattr(ClaudeSession, name, None)), f"{name} missing from ClaudeSession"


def test_check_context_reserve_signature_matches_callers():
    """chat_state.py:634 passes positional `combined`; chat_state.py:769 passes manual=True."""
    sig = inspect.signature(ClaudeSession.check_context_reserve)
    assert "combined" in sig.parameters
    manual = sig.parameters["manual"]
    assert manual.kind is inspect.Parameter.KEYWORD_ONLY
    assert manual.default is False


def test_get_context_usage_signature_matches_callers():
    """chat_state.py:538 calls refresh=True, preserve_session=True."""
    sig = inspect.signature(ClaudeSession.get_context_usage)
    for name in ("refresh", "preserve_session"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_private_disconnect_alias_preserved():
    """chat_state.py:939 still calls the private name — keep it working."""
    assert ClaudeSession._safe_disconnect is ClaudeSession.safe_disconnect


def test_claude_capabilities_declared():
    caps = ClaudeSession.CAPABILITIES
    assert isinstance(caps, RuntimeCapabilities)
    # Claude compaction is Orchestra-side (compact.py), not a native runtime call.
    assert caps.native_compact is False
    assert caps.context_percentage is True
    assert set(caps.to_dict()) == {
        "mid_turn_inject",
        "native_compact",
        "context_percentage",
        "cost_reporting",
        "resume_across_restart",
    }
