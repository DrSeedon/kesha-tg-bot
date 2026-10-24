"""Run unchanged tests with short relative paths for two Unix socket fixtures.

The checkout prefix alone is too long for Linux's 108-byte sockaddr_un limit.
All files still live under the supplied pytest basetemp in this checkout.
Only the spelling of those two temporary paths changes; no assertions or
socket behavior are mocked. The stdio child uses the same checkout cwd.
"""

import sys
from pathlib import Path

import pytest


class RelativeSocketTmp:
    @pytest.fixture
    def tmp_path(self, tmp_path, request):
        if request.node.name in {
            "test_stdio_proxy_sends_file_to_bound_telegram_chat",
            "test_socket_is_owner_only",
        }:
            return tmp_path.relative_to(Path.cwd())
        return tmp_path


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:], plugins=[RelativeSocketTmp()]))
