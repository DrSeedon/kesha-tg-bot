"""Runtime registry — maps a runtime id to a backend factory.

Pattern borrowed from Orchestra (app/runtime_registry.py): a runtime declares
its capabilities up front, and `build_runtime` fails loud at construction time
if the backend does not honour the contract. A runtime that lies about what it
supports must not reach the first user message.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from runtime_protocol import ChatRuntime, RuntimeCapabilities


@dataclass(frozen=True)
class RuntimeBuildContext:
    chat_id: int
    cwd: str
    model: str
    system_prompt: str
    mcp_servers: dict
    session_file: Path
    on_connecting: Callable[[], None] | None = None


@dataclass(frozen=True)
class RuntimeDefinition:
    id: str
    capabilities: RuntimeCapabilities
    factory: Callable[[RuntimeBuildContext], ChatRuntime]


_RUNTIMES: dict[str, RuntimeDefinition] = {}

# Methods every backend must provide — derived from real call sites in
# chat_state.py, response_stream.py, compact.py and handlers.py.
_REQUIRED_METHODS = (
    "send_message",
    "check_context_reserve",
    "get_context_usage",
    "interrupt",
    "reconnect",
    "reset_async",
    "safe_disconnect",
)


def register_runtime(definition: RuntimeDefinition, *, replace: bool = False) -> None:
    if not definition.id:
        raise ValueError("runtime id must not be empty")
    if definition.id in _RUNTIMES and not replace:
        raise ValueError(f"runtime '{definition.id}' is already registered")
    _RUNTIMES[definition.id] = definition


def get_runtime(runtime_id: str) -> RuntimeDefinition:
    try:
        return _RUNTIMES[runtime_id]
    except KeyError:
        known = ", ".join(sorted(_RUNTIMES)) or "none"
        raise ValueError(
            f"unknown runtime '{runtime_id}' (registered: {known})"
        ) from None


def list_runtimes() -> list[str]:
    return sorted(_RUNTIMES)


def build_runtime(runtime_id: str, context: RuntimeBuildContext) -> ChatRuntime:
    definition = get_runtime(runtime_id)
    backend = definition.factory(context)

    missing = [m for m in _REQUIRED_METHODS if not callable(getattr(backend, m, None))]
    if missing:
        raise TypeError(
            f"runtime '{runtime_id}' is missing required methods: {', '.join(missing)}"
        )
    if not isinstance(backend, ChatRuntime):
        raise TypeError(f"runtime '{runtime_id}' does not satisfy ChatRuntime")

    declared = getattr(type(backend), "CAPABILITIES", None)
    if declared is not definition.capabilities:
        raise TypeError(
            f"runtime '{runtime_id}' capabilities disagree with the registry"
        )
    if definition.capabilities.passive_handoff and not callable(
        getattr(backend, "inject_context", None)
    ):
        raise TypeError(
            f"runtime '{runtime_id}' declares passive handoff without inject_context"
        )
    return backend


def _claude_factory(context: RuntimeBuildContext) -> ChatRuntime:
    from claude_session import ClaudeSession

    return ClaudeSession(
        cwd=context.cwd,
        model=context.model,
        system_prompt=context.system_prompt,
        mcp_servers=context.mcp_servers,
        session_file=context.session_file,
        on_connecting=context.on_connecting,
    )


def _codex_factory(context: RuntimeBuildContext) -> ChatRuntime:
    from codex_session import CodexSession

    return CodexSession(
        cwd=context.cwd,
        model=context.model,
        system_prompt=context.system_prompt,
        mcp_servers=context.mcp_servers,
        session_file=context.session_file,
        on_connecting=context.on_connecting,
    )


def _register_builtins() -> None:
    from claude_session import ClaudeSession
    from codex_session import CodexSession

    register_runtime(
        RuntimeDefinition(
            id="claude",
            capabilities=ClaudeSession.CAPABILITIES,
            factory=_claude_factory,
        ),
        replace=True,
    )
    register_runtime(
        RuntimeDefinition(
            id="codex",
            capabilities=CodexSession.CAPABILITIES,
            factory=_codex_factory,
        ),
        replace=True,
    )


_register_builtins()
