"""Test-only fixture for Unix sockets in a deeply nested worker checkout.

All files stay in the task directory. Existing socket/permission/delivery checks
run unchanged; only these two tests receive a shorter relative temporary path.
"""

from pathlib import Path
import tempfile

import pytest


@pytest.fixture
def tmp_path(tmp_path, request):
    if request.node.name not in {
        "test_stdio_proxy_sends_file_to_bound_telegram_chat",
        "test_socket_is_owner_only",
    }:
        yield tmp_path
        return

    parent = Path(__file__).resolve().parent / "tmp" / "u"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="s-") as directory:
        relative = Path(directory).relative_to(Path.cwd())
        assert len(str(relative / "bridge.sock").encode()) < 108
        yield relative
