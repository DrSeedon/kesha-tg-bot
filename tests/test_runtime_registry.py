"""T2 — the registry must fail loud when a runtime lies about its contract."""

from dataclasses import replace
from pathlib import Path

import pytest

from claude_session import ClaudeSession
from runtime_registry import (
    RuntimeBuildContext,
    RuntimeDefinition,
    build_runtime,
    get_runtime,
    list_runtimes,
    register_runtime,
)


def _ctx(tmp_path) -> RuntimeBuildContext:
    return RuntimeBuildContext(
        chat_id=1,
        cwd=str(tmp_path),
        model="claude-opus-5",
        system_prompt="",
        mcp_servers={},
        session_file=tmp_path / "sess",
    )


def test_claude_is_registered():
    assert "claude" in list_runtimes()
    assert get_runtime("claude").capabilities is ClaudeSession.CAPABILITIES


def test_build_claude_returns_working_session(tmp_path):
    session = build_runtime("claude", _ctx(tmp_path))
    assert isinstance(session, ClaudeSession)
    assert session.model == "claude-opus-5"


def test_unknown_runtime_names_the_known_ones():
    with pytest.raises(ValueError, match="unknown runtime 'gpt'"):
        get_runtime("gpt")


def test_duplicate_registration_rejected():
    definition = get_runtime("claude")
    with pytest.raises(ValueError, match="already registered"):
        register_runtime(definition)


def test_backend_missing_contract_method_fails_at_build(tmp_path):
    """A runtime that does not implement the contract must not reach a user message."""

    class Broken:
        CAPABILITIES = ClaudeSession.CAPABILITIES
        model = "x"
        session_id = None

        def send_message(self, text):  # missing everything else
            ...

    register_runtime(
        RuntimeDefinition(
            id="broken",
            capabilities=ClaudeSession.CAPABILITIES,
            factory=lambda _c: Broken(),
        ),
        replace=True,
    )
    with pytest.raises(TypeError, match="missing required methods"):
        build_runtime("broken", _ctx(tmp_path))


def test_capabilities_mismatch_fails_at_build(tmp_path):
    """Registry capabilities and backend declaration must be the same object."""
    lying = replace(ClaudeSession.CAPABILITIES, native_compact=True)
    register_runtime(
        RuntimeDefinition(
            id="liar",
            capabilities=lying,
            factory=_claude_like_factory,
        ),
        replace=True,
    )
    with pytest.raises(TypeError, match="capabilities disagree"):
        build_runtime("liar", _ctx(tmp_path))


def test_passive_handoff_capability_requires_non_agentic_ingress(tmp_path):
    """A flag alone must not make ChatState fall back to an agentic turn."""
    lying = replace(ClaudeSession.CAPABILITIES, passive_handoff=True)

    class LyingClaude(ClaudeSession):
        CAPABILITIES = lying

    register_runtime(
        RuntimeDefinition(
            id="agentic-liar",
            capabilities=lying,
            factory=lambda context: LyingClaude(
                cwd=context.cwd,
                model=context.model,
                session_file=context.session_file,
            ),
        ),
        replace=True,
    )

    with pytest.raises(TypeError, match="without inject_context"):
        build_runtime("agentic-liar", _ctx(tmp_path))


def _claude_like_factory(context: RuntimeBuildContext):
    return ClaudeSession(
        cwd=context.cwd,
        model=context.model,
        session_file=context.session_file,
    )


def test_capabilities_are_immutable():
    with pytest.raises(Exception):
        ClaudeSession.CAPABILITIES.native_compact = True  # type: ignore[misc]


def test_registry_session_file_is_per_chat(tmp_path):
    session = build_runtime("claude", _ctx(tmp_path))
    assert Path(session._session_file) == tmp_path / "sess"
