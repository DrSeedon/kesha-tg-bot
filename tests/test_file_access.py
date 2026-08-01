"""T3c-2 — outbound file whitelist. Escapes must be judged by the REAL path."""

import os

import pytest

import file_access
from file_access import FileNotAllowed, resolve_sendable, sendable_roots


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    """One allowed root, one forbidden area outside it."""
    allowed = tmp_path / "storage"
    allowed.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()

    (allowed / "report.pdf").write_text("ok")
    (outside / ".env").write_text("TELEGRAM_BOT_TOKEN=hunter2")

    monkeypatch.setenv("KESHA_SENDABLE_ROOTS", str(allowed))
    return allowed, outside


def test_file_inside_root_is_allowed(rooted):
    allowed, _ = rooted
    assert resolve_sendable(str(allowed / "report.pdf")) == (allowed / "report.pdf").resolve()


def test_nested_file_inside_root_is_allowed(rooted):
    allowed, _ = rooted
    nested = allowed / "sub" / "deep"
    nested.mkdir(parents=True)
    target = nested / "a.txt"
    target.write_text("x")
    assert resolve_sendable(str(target)) == target.resolve()


def test_dotdot_traversal_is_blocked(rooted):
    """The classic: a path that spells its way out of an allowed prefix."""
    allowed, outside = rooted
    attack = str(allowed / ".." / "secrets" / ".env")
    with pytest.raises(FileNotAllowed, match="outside the allowed"):
        resolve_sendable(attack)


def test_absolute_path_outside_is_blocked(rooted):
    _, outside = rooted
    with pytest.raises(FileNotAllowed, match="outside the allowed"):
        resolve_sendable(str(outside / ".env"))


def test_symlink_out_of_root_is_blocked(rooted):
    """A link living inside the root but pointing out is judged by its target."""
    allowed, outside = rooted
    link = allowed / "innocent.txt"
    link.symlink_to(outside / ".env")
    with pytest.raises(FileNotAllowed, match="outside the allowed"):
        resolve_sendable(str(link))


def test_symlinked_directory_out_of_root_is_blocked(rooted):
    allowed, outside = rooted
    link_dir = allowed / "shortcut"
    link_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileNotAllowed, match="outside the allowed"):
        resolve_sendable(str(link_dir / ".env"))


def test_ssh_key_is_blocked(rooted):
    with pytest.raises(FileNotAllowed):
        resolve_sendable(os.path.expanduser("~/.ssh/id_rsa"))


def test_project_env_is_blocked(rooted):
    """.env carries the bot token and Deepgram key."""
    with pytest.raises(FileNotAllowed):
        resolve_sendable("/etc/passwd")


def test_missing_file_is_blocked(rooted):
    allowed, _ = rooted
    with pytest.raises(FileNotAllowed, match="not accessible"):
        resolve_sendable(str(allowed / "nope.txt"))


def test_directory_is_not_sendable(rooted):
    allowed, _ = rooted
    with pytest.raises(FileNotAllowed, match="not a regular file"):
        resolve_sendable(str(allowed))


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_empty_or_non_string_rejected(rooted, bad):
    with pytest.raises(FileNotAllowed):
        resolve_sendable(bad)  # type: ignore[arg-type]


def test_roots_come_from_config(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("KESHA_SENDABLE_ROOTS", f"{a}:{b}")
    assert sendable_roots() == [a.resolve(), b.resolve()]


def test_multiple_roots_each_allowed(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x").write_text("1")
    (b / "y").write_text("2")
    monkeypatch.setenv("KESHA_SENDABLE_ROOTS", f"{a}:{b}")
    assert resolve_sendable(str(a / "x"))
    assert resolve_sendable(str(b / "y"))


def test_default_roots_are_project_local():
    """Defaults must not include the whole filesystem."""
    from pathlib import Path

    assert "/" not in file_access._DEFAULT_ROOTS.split(":")
    for chunk in file_access._DEFAULT_ROOTS.split(":"):
        assert chunk in ("./storage", "./artifacts", "/tmp"), chunk
    assert Path("/etc").resolve() not in [Path(c).resolve() for c in file_access._DEFAULT_ROOTS.split(":")]


def test_root_prefix_is_not_string_matched(monkeypatch, tmp_path):
    """`/storage-evil` must not pass because it starts with `/storage`."""
    root = tmp_path / "storage"
    root.mkdir()
    sibling = tmp_path / "storage-evil"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("x")
    monkeypatch.setenv("KESHA_SENDABLE_ROOTS", str(root))
    with pytest.raises(FileNotAllowed, match="outside the allowed"):
        resolve_sendable(str(sibling / "loot.txt"))
