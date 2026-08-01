"""Outbound file whitelist — what Kesha is allowed to send into Telegram.

Behind the bridge, a tool taking a free-form path is "read any file on disk and
deliver it to me": `.env` holds the bot token and Deepgram key, `~/.ssh` holds
the laptop tunnel key. Roots are configured, and every candidate is checked
AFTER `Path.resolve()` — comparing the raw string lets `..` and symlinks walk
straight out of an allowed directory.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("kesha.file_access")

_DEFAULT_ROOTS = "./storage:./artifacts:/tmp"


def _configured_roots() -> list[Path]:
    raw = os.getenv("KESHA_SENDABLE_ROOTS", _DEFAULT_ROOTS)
    roots = []
    for chunk in raw.split(":"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            roots.append(Path(chunk).resolve())
        except OSError as exc:
            logger.warning("sendable root %r is unusable: %s", chunk, exc)
    return roots


def sendable_roots() -> list[Path]:
    return _configured_roots()


class FileNotAllowed(Exception):
    """Raised when a path escapes every configured root."""


def resolve_sendable(path: str) -> Path:
    """Return the real path if it is inside an allowed root, else raise.

    `strict=True` resolves symlinks and `..` against the filesystem, so a link
    inside an allowed directory pointing outside is rejected by its TARGET, not
    by how it is spelled.
    """
    if not isinstance(path, str) or not path.strip():
        raise FileNotAllowed("empty path")

    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotAllowed(f"path is not accessible: {exc}") from None

    if not real.is_file():
        raise FileNotAllowed("not a regular file")

    roots = sendable_roots()
    for root in roots:
        if real == root or root in real.parents:
            return real

    logger.warning("blocked outbound file outside allowed roots: %s", real)
    raise FileNotAllowed(
        f"path is outside the allowed directories ({', '.join(str(r) for r in roots)})"
    )
