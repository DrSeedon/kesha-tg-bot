"""Read-only fake-runtime probes for task #34.

The script imports the real ChatState and reserve implementations while
stubbing optional third-party packages.  It never connects to a provider.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
from datetime import time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def install_import_stubs() -> tuple[types.ModuleType, list[str]]:
    user_logs: list[str] = []
    config = types.ModuleType("config")
    config.AUTO_COMPACT_IDLE = timedelta(minutes=55)
    config.AUTO_COMPACT_MIN_CONTEXT_PCT = 20.0
    config.AUTO_COMPACT_TZ = ZoneInfo("Asia/Krasnoyarsk")
    config.AUTO_COMPACT_WINDOW_START = time(23, 0)
    config.AUTO_COMPACT_WINDOW_END = time(8, 0)
    config.PHOTO_CAPTION_WAIT_SEC = 10
    config.RUNTIME_MODELS = {"codex": "fake-codex"}
    config.MODEL = "claude-opus-5"
    config.logger = logging.getLogger("probe")
    config.STRINGS = {
        "ru": {
            "context_reserve": "RESERVE_TERMINAL",
            "session_unavailable": "SESSION_UNAVAILABLE",
            "context_usage_limit": "USAGE_LIMIT",
            "context_runtime_invariant": "RUNTIME_INVARIANT {expected}",
            "context_runtime_unhealthy": "RUNTIME_UNHEALTHY",
            "context_unknown": "UNKNOWN",
            "compact_floor": "COMPACT_FLOOR",
            "compact_native_start": "START {runtime} {before:.0f}",
            "compact_native_done": "DONE {runtime} {before:.0f} {after:.0f} {tokens}",
            "compact_native_done_unmeasured": "DONE_UNMEASURED {runtime} {before:.0f}",
            "compact_native_failed": "FAILED {error}",
        }
    }

    def render(key, lang="ru", **fmt):
        return config.STRINGS[lang][key].format(**fmt)

    config.render = render
    config.t = lambda _message, key, **fmt: render(key, **fmt)
    sys.modules["config"] = config

    message_log = types.ModuleType("message_log")

    class ActivityPersistenceError(RuntimeError):
        pass

    class DB:
        def log_user(self, _chat_id, content, _message_id):
            user_logs.append(content)

        def get_history(self, _chat_id, limit=12):
            return []

    message_log.ActivityPersistenceError = ActivityPersistenceError
    message_log.get_db = lambda: DB()
    sys.modules["message_log"] = message_log

    sdk = types.ModuleType("claude_agent_sdk")

    class Options:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    for name in (
        "ClaudeSDKClient",
        "AssistantMessage",
        "ResultMessage",
        "RateLimitEvent",
        "StreamEvent",
        "SystemMessage",
        "TextBlock",
        "ToolUseBlock",
        "ToolResultBlock",
        "PermissionResultAllow",
    ):
        setattr(sdk, name, type(name, (), {}))
    sdk.ClaudeAgentOptions = Options
    sdk.McpSdkServerConfig = dict
    sys.modules["claude_agent_sdk"] = sdk

    quota = types.ModuleType("quota_gate")
    quota.claude_windows = lambda value: value
    quota.codex_windows = lambda value: value
    quota.fetch_claude_usage = lambda force=False: None
    quota.quota_exhausted = lambda value: False
    sys.modules["quota_gate"] = quota
    return config, user_logs


CONFIG, USER_LOGS = install_import_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import chat_state  # noqa: E402
import claude_session  # noqa: E402
import codex_session  # noqa: E402
from runtime_protocol import RuntimeCapabilities  # noqa: E402


class FakeClaudeUsageClient:
    def __init__(self, usage):
        self.usage = usage
        self.queries: list[str] = []

    async def get_context_usage(self):
        return self.usage


async def probe_reserve_math() -> None:
    root = Path(tempfile.mkdtemp(prefix="task34-reserve-"))
    claude = claude_session.ClaudeSession(cwd=str(root), session_file=root / "sid")
    claude._connected = True
    usage = {
        "totalTokens": 790_000,
        "maxTokens": 1_000_000,
        "rawMaxTokens": 1_000_000,
        "model": claude.expected_context_model,
        "isAutoCompactEnabled": False,
        "percentage": 79.0,
    }
    client = FakeClaudeUsageClient(usage)
    claude._client = client
    exact = await claude.check_context_reserve("x" * 2_000)
    over = await claude.check_context_reserve("x" * 2_001)
    print(
        "claude_79pct",
        f"remaining={exact['remaining']}",
        f"required_2000={exact['required']}",
        f"admit_2000={exact['ok']}",
        f"required_2001={over['required']}",
        f"admit_2001={over['ok']}",
        f"queries={len(client.queries)}",
    )

    codex = codex_session.CodexSession(cwd=str(root), session_file=root / "codex-sid")

    async def connected():
        return None

    codex._connect = connected
    codex._context_window = 258_400
    codex._context_tokens = 236_056
    blocked = await codex.check_context_reserve("Ку")
    codex._context_tokens = None
    admitted = await codex.check_context_reserve("Ку")
    print(
        "codex_stale_gauge",
        f"pct={236_056 / 258_400 * 100:.1f}",
        f"blocked_reason={blocked['reason']}",
        f"after_verified_compact_unknown_admits={admitted['ok']}",
        f"remaining_is_none={admitted['remaining'] is None}",
    )


class Store:
    def begin_activity(self, *_args, **_kwargs):
        return ""

    def finish_activity(self, *_args, **_kwargs):
        return ""

    def get_activity(self, _chat_id):
        return None

    def claim_auto_attempt(self, *_args):
        return False


class Bot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, _chat_id, text, **_kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, *_args, **_kwargs):
        return None


class ReserveRejectingRuntime:
    CAPABILITIES = RuntimeCapabilities(False, False, True, False, True)
    session_id = None
    model = "fake"
    usage_limit_active = False

    def __init__(self):
        self.reserve_calls = 0

    async def check_context_reserve(self, _combined="", *, manual=False):
        self.reserve_calls += 1
        return {"ok": False, "reason": "reserve"}


def make_state(runtime, ask, compact):
    return chat_state.ChatState(
        chat_id=7,
        session=runtime,
        bot=Bot(),
        debounce_sec=1,
        ask_fn=ask,
        set_current_chat_fn=lambda _chat_id: None,
        get_lazy_block_fn=lambda _chat_id: ("", [], []),
        compact_session_fn=compact,
        activity_store=Store(),
        work_dir="/tmp",
    )


async def probe_current_batch_fate() -> None:
    asks: list[str] = []

    async def ask(_message, prompt, _chat_id):
        asks.append(prompt)

    async def compact(*_args, **_kwargs):
        return {"ok": True, "before_pct": 96, "after_pct": 4}

    runtime = ReserveRejectingRuntime()
    state = make_state(runtime, ask, compact)
    state.phase = chat_state.ChatPhase.PROCESSING
    entry = chat_state.PendingEntry("ORIGINAL", 34, None, "user", 7)
    USER_LOGS.clear()
    await state._run_batch([entry])
    print(
        "current_rejection",
        f"reserve_calls={runtime.reserve_calls}",
        f"ask_calls={len(asks)}",
        f"user_log_calls={len(USER_LOGS)}",
        f"terminal_notices={len(state.bot.sent)}",
        f"latched={state._context_reserve_blocked}",
        f"pending={len(state.pending)}",
        f"deferred={len(state.deferred)}",
        f"phase={state.phase}",
    )


async def probe_compact_helper_is_not_nested_transaction() -> None:
    dispatched: list[list[str]] = []

    async def ask(*_args):
        return None

    async def compact(*_args, **_kwargs):
        return {"ok": True, "before_pct": 96, "after_pct": 4}

    runtime = ReserveRejectingRuntime()
    state = make_state(runtime, ask, compact)
    state.phase = chat_state.ChatPhase.COMPACTING
    state._compact_started = True
    state.deferred = [[chat_state.PendingEntry("LATER", 35, None, "user", 7)]]

    async def capture_start(batch):
        dispatched.append([entry.prompt for entry in batch])

    state._start_processing = capture_start
    await state._do_compact(automatic=True)
    print(
        "nested_current_do_compact",
        f"deferred_dispatched={dispatched}",
        f"phase={state.phase}",
        "outer_original_not_owned_by_helper=True",
    )


class TransactionRuntime:
    def __init__(self, name: str, *, compact_ok: bool = True):
        self.name = name
        self.compact_ok = compact_ok
        self.usage = [96.0, 4.0]
        self.events: list[str] = []

    async def measure(self, prompt: str) -> float:
        self.events.append(f"measure:{prompt}")
        return self.usage[min(self.events.count(f"measure:{prompt}") - 1, 1)]

    async def compact(self) -> bool:
        self.events.append(f"compact:{self.name}")
        return self.compact_ok

    async def send(self, prompt: str) -> None:
        self.events.append(f"send:{prompt}")


async def bounded_preflight(runtime: TransactionRuntime, batch: list[str]) -> dict:
    """Executable model of the candidate single-owner transaction.

    One coroutine retains the admitted batch, permits one compact attempt, and
    sends the exact batch only after a successful post-compact measurement.
    """
    prompt = "\n".join(batch)
    compact_attempted = False
    pct = await runtime.measure(prompt)
    if pct >= 95.0:
        compact_attempted = True
        if not await runtime.compact():
            return {"sent": False, "retained": batch, "attempted": compact_attempted}
        pct = await runtime.measure(prompt)
        if pct >= 95.0:
            return {"sent": False, "retained": batch, "attempted": compact_attempted}
    await runtime.send(prompt)
    return {"sent": True, "retained": None, "attempted": compact_attempted}


async def probe_candidate_transaction() -> None:
    for name in ("claude-replacement", "codex-native"):
        runtime = TransactionRuntime(name)
        batch = ["ORIGINAL-34"]
        result = await bounded_preflight(runtime, batch)
        print(
            "candidate_success",
            name,
            f"events={runtime.events}",
            f"sent_exactly_once={runtime.events.count('send:ORIGINAL-34') == 1}",
            f"same_object_retained_until_send={result['retained'] is None}",
        )

    failed = TransactionRuntime("claude-replacement", compact_ok=False)
    batch = ["ORIGINAL-FAIL"]
    result = await bounded_preflight(failed, batch)
    print(
        "candidate_failure",
        f"events={failed.events}",
        f"sent={result['sent']}",
        f"same_batch_retained={result['retained'] is batch}",
        f"compact_attempts={sum(e.startswith('compact:') for e in failed.events)}",
    )


async def main() -> None:
    await probe_reserve_math()
    await probe_current_batch_fate()
    await probe_compact_helper_is_not_nested_transaction()
    await probe_candidate_transaction()


if __name__ == "__main__":
    asyncio.run(main())
