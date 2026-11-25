"""Outbound file whitelist — what Kesha is allowed to send into Telegram.

Behind the bridge, a tool taking a free-form path is "read any file on disk and
deliver it to me": `.env` holds the bot token and Deepgram key, `~/.ssh` holds
the laptop tunnel key. Roots are configured, and every candidate is checked
AFTER `Path.resolve()` — comparing the raw string lets `..` and symlinks walk
straight out of an allowed directory.
"""

import logging
import os
import stat as _stat
from pathlib import Path

logger = logging.getLogger("kesha.file_access")

# Cloud Bot API caps documents at 50MB; local telegram-bot-api (--local) raises
# the cap to 2000MB. Keep RAM-buffered reads <= 50MB and stream larger files.
CLOUD_MAX_SEND_BYTES = 50 * 1024 * 1024
LOCAL_MAX_SEND_BYTES = 2000 * 1024 * 1024


def max_send_bytes() -> int:
    return LOCAL_MAX_SEND_BYTES if os.getenv("TELEGRAM_API_BASE", "").strip() else CLOUD_MAX_SEND_BYTES


MAX_SEND_BYTES = CLOUD_MAX_SEND_BYTES

# Not bare /tmp: on the production host it is shared with other services, and a
# default must be safe on its own rather than safe-if-nothing-else-is-breached.
_DEFAULT_ROOTS = "./storage:./artifacts:/tmp/kesha"


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


def ensure_roots() -> None:
    """Create configured roots at startup so legitimate paths resolve.

    `resolve(strict=True)` refuses a path under a directory that does not exist,
    so a missing scratch dir would block real sends, not just attacks.
    """
    for root in _configured_roots():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create sendable root %s: %s", root, exc)


class FileNotAllowed(Exception):
    """Raised when a path escapes every configured root."""


def _link_stays_inside(real: Path, info: os.stat_result) -> bool:
    """Whether a multi-linked file has all its names inside the allowed roots.

    `resolve()` cannot help here: a hard link is not a pointer to a target, it
    is an equal name for the same inode, so `/tmp/kesha/x` hard-linked to
    `~/.ssh/id_rsa` resolves to itself and passes the path check (measured).
    Rather than scan the filesystem, require that a multi-linked file be
    reachable only from inside a root — the common case (one link) is free.
    """
    seen: set[Path] = set()
    for root in sendable_roots():
        for candidate in root.rglob("*"):
            try:
                # A symlink is not another hard-link name. Count each real
                # directory entry once even when configured roots overlap.
                cand = candidate.lstat()
                if not _stat.S_ISREG(cand.st_mode):
                    continue
            except OSError:
                continue
            if (cand.st_dev, cand.st_ino) == (info.st_dev, info.st_ino):
                seen.add(candidate)
                if len(seen) >= info.st_nlink:
                    return True
    return False


def validate_sendable_path(path: str) -> tuple[Path, int]:
    """Validate path and size for streaming delivery (used for large local-API files)."""
    real = resolve_sendable(path)
    fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise FileNotAllowed("not a regular file")
        if info.st_nlink > 1 and not _link_stays_inside(real, info):
            raise FileNotAllowed("file is hard-linked outside the allowed directories")
        limit = max_send_bytes()
        if info.st_size > limit:
            raise FileNotAllowed(
                f"file is larger than the {limit // (1024 * 1024)}MB limit"
            )
        return real, info.st_size
    finally:
        os.close(fd)


def open_sendable(path: str) -> tuple[Path, bytes]:
    """Validate and read in one step, closing the check-to-use window.

    `resolve_sendable` alone is not enough for delivery: aiogram's FSInputFile
    stores the path and opens it later, so a symlink swapped in between passes
    validation and leaks the new target (reproduced: a checked file became a
    link to the token file before the read). Reading here means the bytes we
    validated are the bytes we send.
    """
    real = resolve_sendable(path)
    fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise FileNotAllowed("not a regular file")
        if info.st_nlink > 1 and not _link_stays_inside(real, info):
            raise FileNotAllowed("file is hard-linked outside the allowed directories")
        if info.st_size > MAX_SEND_BYTES:
            raise FileNotAllowed(
                f"file is larger than the {MAX_SEND_BYTES // (1024 * 1024)}MB limit"
            )
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return real, b"".join(chunks)


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
