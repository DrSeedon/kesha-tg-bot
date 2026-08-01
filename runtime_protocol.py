"""Runtime protocol — structural typing contract for chat backends (Claude, Codex).

Narrow by design (see docs/tasks/16/): compaction is NOT part of this contract.
Each runtime implements its own compaction internally; the caller dispatches on
`RuntimeCapabilities.native_compact` instead of a shared transaction API.
"""

from dataclasses import dataclass
from typing import AsyncGenerator, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What a runtime can and cannot do. Declared, never silently degraded."""

    mid_turn_inject: bool
    native_compact: bool
    context_percentage: bool
    cost_reporting: bool
    resume_across_restart: bool

    def to_dict(self) -> dict:
        return {
            "mid_turn_inject": self.mid_turn_inject,
            "native_compact": self.native_compact,
            "context_percentage": self.context_percentage,
            "cost_reporting": self.cost_reporting,
            "resume_across_restart": self.resume_across_restart,
        }


@runtime_checkable
class ChatRuntime(Protocol):
    """Contract every chat backend must satisfy.

    Derived from the real call sites, not from an idealized design:
    handlers.py, chat_state.py, response_stream.py and compact.py each depend
    on some part of this surface. Shrinking it breaks a caller at runtime.
    """

    model: str

    @property
    def session_id(self) -> Optional[str]: ...

    def send_message(self, text: str) -> AsyncGenerator[dict, None]:
        """Yield normalized chunks: text_delta | text | tool | result | turn_done | error.

        An async generator, not a coroutine — callers use `async for` directly.
        """
        ...

    async def check_context_reserve(
        self,
        combined: str = "",
        *,
        manual: bool = False,
    ) -> dict:
        """Fail closed unless there is proven headroom. Called before EVERY turn."""
        ...

    async def get_context_usage(
        self,
        *,
        refresh: bool = False,
        preserve_session: bool = False,
    ) -> Optional[dict]: ...

    async def interrupt(self) -> None: ...

    def reconnect(self) -> None: ...

    async def reset_async(self) -> None: ...

    async def safe_disconnect(self) -> None: ...
