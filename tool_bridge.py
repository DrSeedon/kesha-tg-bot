"""Tool bridge — lets an out-of-process MCP server invoke in-process bot tools.

Security model (task #16, T3a). The bridge is a privileged surface: behind it sit
`send_file` (reads a path off disk) and `run_on_laptop` (SSH to the user's laptop).
Three rules make it safe to expose:

1. **chat_id is never an argument.** The caller cannot choose a destination chat;
   the bridge resolves it from trusted server-side state. A hallucinating or
   prompt-injected model must not be able to send someone else's chat a file.
2. **A capability token is required.** A unix socket alone is not authorization —
   any local process can connect to it. The token is generated at startup, never
   logged, and compared in constant time.
3. **Unix socket, not TCP.** Filesystem permissions (0600) narrow the surface
   before authentication even runs. Loopback TCP is reachable by every local user.

Argument validation (paths, laptop commands) stays with the in-process tool
implementations — the bridge transports calls, it never widens trust.
"""

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("kesha.bridge")

SOCKET_PATH = Path(
    os.getenv("KESHA_BRIDGE_SOCKET", "./storage/bridge.sock")
).resolve()
TOKEN_ENV = "KESHA_BRIDGE_TOKEN"
_TOKEN_HEADER = "X-Kesha-Bridge-Token"

ToolHandler = Callable[[dict], Awaitable[dict]]


def normalize_arg_name(key: object) -> str | None:
    """Return a comparable argument name, or None if it can never be valid.

    Blacklisting `chat_id` spellings is a race we cannot win: ` chat_id`,
    `chat-id`, a Cyrillic `с`, or a zero-width space all read as the same word
    to a human and as different keys to `dict`. So names are normalized here and
    matched against a per-tool whitelist instead. Non-ASCII is rejected outright
    — every real argument name in this codebase is ASCII, and a homoglyph is
    never a legitimate parameter.
    """
    if not isinstance(key, str):
        return None
    cleaned = "".join(ch for ch in key if not _is_invisible(ch)).strip()
    if not cleaned.isascii():
        return None
    return cleaned.casefold().replace("-", "_")


def _is_invisible(ch: str) -> bool:
    # Zero-width and bidi controls carry no meaning in an argument name but do
    # change dict identity, so they are stripped before comparison.
    return ch in "​‌‍⁠﻿‎‏" or (
        ch.isspace() and ch not in " \t"
    )


class ToolBridge:
    """Serves in-process tool handlers over an authenticated unix socket."""

    def __init__(self, token: str, resolve_chat: Callable[[], int | None]):
        if not token:
            raise ValueError("bridge token must not be empty")
        self._token = token
        self._resolve_chat = resolve_chat
        self._handlers: dict[str, ToolHandler] = {}
        self._allowed_args: dict[str, frozenset[str]] = {}
        self._runner: web.AppRunner | None = None

    def register(
        self,
        name: str,
        handler: ToolHandler,
        allowed_args: frozenset[str] | set[str] | tuple[str, ...] = (),
    ) -> None:
        """Register a tool and the exact argument names it accepts.

        `allowed_args` is a whitelist: anything else is refused before the
        handler runs. This covers arguments nobody thought to forbid, not just
        the ones we remembered.
        """
        if name in self._handlers:
            raise ValueError(f"tool '{name}' already registered")
        normalized = set()
        for arg in allowed_args:
            clean = normalize_arg_name(arg)
            if clean is None:
                raise ValueError(f"tool '{name}' declares invalid argument {arg!r}")
            normalized.add(clean)
        self._handlers[name] = handler
        self._allowed_args[name] = frozenset(normalized)

    @property
    def tools(self) -> list[str]:
        return sorted(self._handlers)

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get(_TOKEN_HEADER, "")
        # compare_digest on str requires ASCII; a non-ASCII header is never valid.
        try:
            return hmac.compare_digest(supplied, self._token)
        except TypeError:
            return False

    async def handle(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            # Do not reveal whether the tool exists to an unauthenticated caller.
            logger.warning("bridge: rejected unauthenticated call")
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        name = payload.get("tool", "")
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            return web.json_response({"error": "args must be an object"}, status=400)

        handler = self._handlers.get(name)
        if handler is None:
            return web.json_response({"error": f"unknown tool '{name}'"}, status=404)

        allowed = self._allowed_args[name]
        clean_args: dict = {}
        for raw_key, value in args.items():
            clean_key = normalize_arg_name(raw_key)
            if clean_key is None or clean_key not in allowed:
                logger.warning(
                    "bridge: rejected argument %r for %s", raw_key, name
                )
                return web.json_response(
                    {"error": f"argument not accepted: {raw_key!r}"}, status=400
                )
            if clean_key in clean_args:
                return web.json_response(
                    {"error": f"duplicate argument: {clean_key!r}"}, status=400
                )
            clean_args[clean_key] = value

        if self._resolve_chat() is None:
            return web.json_response({"error": "no active chat context"}, status=409)

        try:
            # Handlers receive normalized keys only — never the caller's spelling.
            result = await handler(clean_args)
        except Exception as exc:
            logger.error("bridge: tool %s failed: %s", name, exc, exc_info=True)
            return web.json_response(
                {"error": f"{type(exc).__name__}: {exc}"}, status=500
            )
        return web.json_response({"result": result})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/tool", self.handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()

        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOCKET_PATH.unlink(missing_ok=True)
        site = web.UnixSite(self._runner, str(SOCKET_PATH))
        await site.start()
        # Narrow the surface before auth runs: owner-only access.
        os.chmod(SOCKET_PATH, 0o600)
        logger.info(
            "bridge: listening on %s (%d tools)", SOCKET_PATH, len(self._handlers)
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        SOCKET_PATH.unlink(missing_ok=True)


def issue_token() -> str:
    """Return the bridge token, generating one if the environment has none.

    Generated tokens are passed to the MCP subprocess via its environment, so a
    restart of the bot invalidates old ones automatically.
    """
    existing = os.getenv(TOKEN_ENV, "").strip()
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    os.environ[TOKEN_ENV] = token
    return token
