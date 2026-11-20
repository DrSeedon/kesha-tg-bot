"""Fast offline ExecStartPre import smoke check for immutable Kesha releases."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure dry import checks never require network or touch Google CLI.
os.environ.setdefault("KESHA_SMOKE_CHECK", "1")
try:
    from dotenv import load_dotenv
    if (ROOT / ".env").exists():
        load_dotenv(ROOT / ".env")
except Exception:
    pass
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

MODULES = (
    "config",
    "runtime_protocol",
    "runtime_registry",
    "model_choices",
    "quota_gate",
    "antigravity_session",
    "codex_session",
    "claude_session",
    "chat_state",
    "inbox_server",
    "orch_ops",
    "skillcache_trace",
    "handlers",
    "kesha_tools",
    "bot",
)


def main() -> int:
    for mod in MODULES:
        __import__(mod)
    print(f"SMOKE_IMPORTS_OK={len(MODULES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
