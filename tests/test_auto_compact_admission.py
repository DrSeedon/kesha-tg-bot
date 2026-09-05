"""Frozen RED acceptance oracles for task #34.

These tests describe the product boundary, not the current reserve/latch
implementation.  The admitted batch is the owner across an automatic compact;
only after its one runtime query completes may deferred work drain.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import config
from chat_state import ChatPhase, ChatState, PendingEntry
from claude_session import MANUAL_COMPACT_FLOOR_TOKENS
from runtime_protocol import RuntimeCapabilities


class Store:
    def __init__(self, row=None):
        self.row = row

    def begin_activity(self, *_args, **_kwargs):
        return ""

    def finish_activity(self, *_args, **_kwargs):
        return ""

    def get_activity(self, _chat_id):
        return self.row

    def claim_auto_attempt(self, *_args):
        return False


class Bot:
    def __init__(self):
        self.sent: list[str] = []
        self.edited: list[str] = []

    async def send_message(self, _chat_id, text, **_kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, **_kwargs):
        self.edited.append(text)

    async def delete_message(self, *_args, **_kwargs):
        return None


def capabilities(*, native: bool) -> RuntimeCapabilities:
    return RuntimeCapabilities(
        mid_turn_inject=False,
        native_compact=native,
        context_percentage=not native,
        cost_reporting=False,
        resume_across_restart=True,
    )


class Runtime:
    CAPABILITIES = capabilities(native=False)

    def __init__(
        self,
        usage: dict | None,
        *,
        native: bool = False,
        legacy_reserve: dict | None = None,
    ):
        if native:
            self.CAPABILITIES = capabilities(native=True)
        self.usage = usage
        self.model = "fake"
        self.session_id = None
        self.usage_limit_active = False
        self.legacy_reserve = legacy_reserve or {
            "ok": False,
            "reason": "reserve",
        }
        self.native_compact_calls = 0
        self.native_started = asyncio.Event()
        self.native_release = asyncio.Event()
        self.block_native = False

    async def get_context_usage(self, *, refresh=False, preserve_session=False):
        return self.usage

    async def check_context_reserve(self, combined="", *, manual=False):
        if manual:
            return {"ok": True, "reason": None, "remaining": 900_000}
        trigger = getattr(config, "AUTO_COMPACT_TRIGGER_PCT", None)
        if trigger is not None and self.usage is not None:
            maximum = self.usage["maxTokens"]
            prompt_tokens = (
                max(1, len(combined) // 2)
                if self.CAPABILITIES.native_compact
                else len(combined.encode("utf-8"))
            )
            projected = self.usage["totalTokens"] + prompt_tokens
            runtime_trigger = (
                getattr(config, "CODEX_AUTO_COMPACT_TRIGGER_PCT", 90.0)
                if self.CAPABILITIES.native_compact
                else trigger
            )
            return {
                "ok": True,
                "reason": None,
                "should_compact": projected >= maximum * runtime_trigger / 100,
                "projected_tokens": projected,
                "max_tokens": maximum,
            }
        if trigger is not None and self.usage is None:
            return {
                "ok": True,
                "reason": None,
                "should_compact": False,
                "projected_tokens": None,
                "max_tokens": None,
            }
        return self.legacy_reserve

    async def compact_context(self):
        self.native_compact_calls += 1
        if self.native_compact_calls > 1:
            raise AssertionError("native compact loop exceeded one attempt")
        self.native_started.set()
        if self.block_native:
            await self.native_release.wait()
        self.usage = None
        return {
            "completed": True,
            "context_tokens": None,
            "max_tokens": 258_400,
            "measured_after": False,
        }

    async def interrupt(self):
        return None

    def reconnect(self):
        return None

    async def reset_async(self):
        return None

    async def safe_disconnect(self):
        return None

    async def send_message(self, _text):
        yield {"type": "turn_done"}


class CompactDriver:
    def __init__(
        self,
        runtime: Runtime,
        *,
        outcome: dict | None = None,
        after_usage: dict | None | object = Ellipsis,
        block: bool = False,
    ):
        self.runtime = runtime
        self.outcome = outcome or {
            "ok": True,
            "before_pct": 95.0,
            "after_pct": 4.0,
        }
        self.after_usage = after_usage
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block

    async def __call__(self, _session, notify=None, recent_rows=None):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("custom compact loop exceeded one attempt")
        self.started.set()
        if notify:
            await notify("AUTO COMPACT START", replace=True)
        if self.block:
            await self.release.wait()
        if self.outcome.get("ok") and self.after_usage is not Ellipsis:
            self.runtime.usage = self.after_usage
        if notify:
            await notify(
                "AUTO COMPACT DONE" if self.outcome.get("ok") else "AUTO COMPACT FAILED",
                replace=True,
            )
        return dict(self.outcome)


def usage(total: int, maximum: int = 1_000_000) -> dict:
    return {
        "totalTokens": total,
        "maxTokens": maximum,
        "rawMaxTokens": maximum,
        "percentage": total / maximum * 100,
        "model": "fake",
        "isAutoCompactEnabled": False,
    }


def entry(text: str, message_id: int) -> PendingEntry:
    return PendingEntry(
        prompt=text,
        message_id=message_id,
        message=None,
        source="user",
        reply_target=7,
    )


def make_state(runtime, compact, ask, *, store=None, runtime_id="claude"):
    return ChatState(
        chat_id=7,
        session=runtime,
        bot=Bot(),
        debounce_sec=60,
        ask_fn=ask,
        set_current_chat_fn=lambda _chat_id: None,
        get_lazy_block_fn=lambda _chat_id: ("", [], []),
        compact_session_fn=compact,
        activity_store=store or Store(),
        work_dir="/tmp",
        runtime_id=runtime_id,
    )


@pytest.mark.asyncio
async def test_t1_79_percent_ordinary_prompt_is_sent_without_compact_or_rejection(
    monkeypatch,
):
    """The exact old 79%/2,001-byte reserve boundary must disappear."""
    runtime = Runtime(usage(790_000))
    compact = CompactDriver(runtime)
    asked: list[str] = []

    async def ask(_message, prompt, _chat_id):
        asked.append(prompt)

    state = make_state(runtime, compact, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_args, **_kwargs: None),
    )

    await state._run_batch([entry("x" * 2_001, 34)])

    assert compact.calls == 0
    assert len(asked) == 1, "the admitted 79% batch was not sent"
    assert "x" * 2_001 in asked[0]
    assert not state.bot.sent, "an ordinary 79% batch produced a terminal notice"


def test_t1_claude_trigger_leaves_room_for_the_compact_turn_itself():
    """The trigger is bounded by the measured cost of one compact, not by habit."""
    trigger = getattr(config, "AUTO_COMPACT_TRIGGER_PCT", None)
    codex_trigger = getattr(config, "CODEX_AUTO_COMPACT_TRIGGER_PCT", None)

    assert MANUAL_COMPACT_FLOOR_TOKENS == 16_000
    assert trigger == 95.0
    assert codex_trigger == 90.0
    # The trigger must leave room for the compact turn itself, measured at
    # 5-8K tokens per summary: 95% leaves 50_000, over 3x the floor.
    assert int(1_000_000 * (100 - trigger) / 100) >= 3 * MANUAL_COMPACT_FLOOR_TOKENS


@pytest.mark.asyncio
async def test_t1_predicted_boundary_compacts_once_then_sends_original_once(
    monkeypatch,
):
    runtime = Runtime(usage(948_500), legacy_reserve={"ok": True, "reason": None})
    compact = CompactDriver(runtime, after_usage=usage(40_000))
    asked: list[str] = []

    async def ask(_message, prompt, _chat_id):
        asked.append(prompt)

    state = make_state(runtime, compact, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_args, **_kwargs: None),
    )

    await state._run_batch([entry("ORIGINAL-BOUNDARY-" + "x" * 2_000, 35)])

    assert compact.calls == 1
    assert len(asked) == 1
    assert "ORIGINAL-BOUNDARY" in asked[0]


@pytest.mark.asyncio
async def test_t1_unrunnable_compact_still_sends_the_admitted_batch(monkeypatch):
    """A compact we cannot run must not silently swallow the user's message."""
    runtime = Runtime(usage(948_500), legacy_reserve={"ok": True, "reason": None})
    runtime.usage_limit_active = True
    compact = CompactDriver(runtime, after_usage=usage(40_000))
    asked: list[str] = []

    async def ask(_message, prompt, _chat_id):
        asked.append(prompt)

    state = make_state(runtime, compact, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_args, **_kwargs: None),
    )

    await state._run_batch([entry("LIMIT-LATCH-" + "x" * 2_000, 36)])

    assert compact.calls == 0, "the usage limit did not stop the compact"
    assert len(asked) == 1, "the admitted batch was dropped when compact failed"
    assert "LIMIT-LATCH" in asked[0]
    assert not state.bot.sent, "the batch was answered with a terminal notice"


@pytest.mark.asyncio
async def test_t1_arrival_during_compact_stays_behind_original(monkeypatch):
    runtime = Runtime(usage(949_000), legacy_reserve={"ok": True, "reason": None})
    compact = CompactDriver(
        runtime,
        after_usage=usage(40_000),
        block=True,
    )
    order: list[str] = []
    original_started = asyncio.Event()
    original_release = asyncio.Event()

    async def ask(_message, prompt, _chat_id):
        label = "ORIGINAL" if "ORIGINAL" in prompt else "LATER"
        order.append(label)
        if label == "ORIGINAL":
            original_started.set()
            await original_release.wait()

    state = make_state(runtime, compact, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_args, **_kwargs: None),
    )
    task = asyncio.create_task(
        state._run_batch([entry("ORIGINAL-" + "x" * 2_000, 36)])
    )

    for _ in range(20):
        if compact.started.is_set():
            break
        await asyncio.sleep(0)
    if not compact.started.is_set():
        original_release.set()
        await task
    assert compact.started.is_set(), "predicted boundary did not enter compact"

    await state.accept_entry(entry("LATER", 37))
    assert [batch[0].prompt for batch in state.deferred] == ["LATER"]
    compact.release.set()
    await original_started.wait()
    assert order == ["ORIGINAL"]
    assert state.deferred, "deferred work drained before original completed"

    original_release.set()
    await task
    if state._processing_task:
        await state._processing_task
    assert order == ["ORIGINAL", "LATER"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["compact", "unchanged", "oversized"])
async def test_t1_failure_is_one_bounded_terminal_with_zero_send_or_loop(
    monkeypatch,
    failure,
):
    prompt = "ORIGINAL-FAIL-" + ("x" * (930_000 if failure == "oversized" else 2_000))
    runtime = Runtime(usage(949_000))
    if failure == "compact":
        compact = CompactDriver(
            runtime,
            outcome={"ok": False, "reason": "summary_error"},
        )
    elif failure == "unchanged":
        compact = CompactDriver(runtime, after_usage=usage(949_000))
    else:
        compact = CompactDriver(runtime, after_usage=usage(40_000))
    asked: list[str] = []

    async def ask(_message, text, _chat_id):
        asked.append(text)

    state = make_state(runtime, compact, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_args, **_kwargs: None),
    )

    await state._run_batch([entry(prompt, 38)])

    visible = state.bot.sent + state.bot.edited
    assert compact.calls == 1, "compact retry loop was not bounded to one attempt"
    assert asked == []
    assert state.phase is ChatPhase.IDLE
    assert visible, "failure had no bounded terminal"
    assert all("/compact" not in text for text in visible)
    assert all("повтор" not in text.casefold() and "resend" not in text.casefold() for text in visible)


@pytest.mark.asyncio
async def test_t2_codex_original_consumes_first_unknown_post_compact_admission(
    monkeypatch,
):
    runtime = Runtime(usage(236_056, 258_400), native=True)
    runtime.session_id = "codex-thread"
    runtime.block_native = True
    custom = CompactDriver(runtime)
    order: list[str] = []
    unknown_consumers: list[str] = []
    original_started = asyncio.Event()
    original_release = asyncio.Event()

    async def ask(_message, prompt, _chat_id):
        label = "ORIGINAL" if "ORIGINAL" in prompt else "LATER"
        order.append(label)
        if runtime.usage is None:
            unknown_consumers.append(label)
        if label == "ORIGINAL":
            runtime.usage = usage(30_000, 258_400)
            original_started.set()
            await original_release.wait()

    state = make_state(runtime, custom, ask, runtime_id="codex")
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_args, **_kwargs: None),
    )
    task = asyncio.create_task(state._run_batch([entry("ORIGINAL-CODEX", 39)]))

    for _ in range(20):
        if runtime.native_started.is_set():
            break
        await asyncio.sleep(0)
    assert runtime.native_started.is_set(), "Codex admission did not compact"

    await state.accept_entry(entry("LATER", 40))
    runtime.native_release.set()
    await original_started.wait()
    assert order == ["ORIGINAL"]
    assert unknown_consumers == ["ORIGINAL"]
    assert state.deferred

    original_release.set()
    await task
    if state._processing_task:
        await state._processing_task
    assert order == ["ORIGINAL", "LATER"]
    assert runtime.native_compact_calls == 1
    assert custom.calls == 0


@pytest.mark.asyncio
async def test_t3_night_55m_20pct_no_longer_arms_or_compacts():
    runtime = Runtime(usage(200_000))
    runtime.session_id = "claude-session"
    compact = CompactDriver(runtime)

    async def ask(*_args):
        return None

    store = Store(
        {
            "quiescent": 1,
            "auto_attempted_for_utc": None,
            "last_activity_utc": "2026-09-03T16:00:00+00:00",
        }
    )
    state = make_state(runtime, compact, ask, store=store)
    arm = getattr(state, "_arm_auto_compact", None)
    if arm:
        arm()
    task = getattr(state, "_auto_compact_task", None)
    if task:
        task.cancel()

    assert task is None, "the independent night scheduler still armed"
    assert compact.calls == 0


def test_t3_legacy_reserve_terminal_and_latch_are_removed():
    runtime = Runtime(usage(100_000))

    async def compact(*_args, **_kwargs):
        return {"ok": True}

    async def ask(*_args):
        return None

    state = make_state(runtime, compact, ask)
    source = inspect.getsource(ChatState._run_batch)

    assert 'reason == "reserve"' not in source
    assert "_context_reserve_blocked" not in source
    assert not hasattr(state, "_context_reserve_blocked")
    assert not hasattr(state, "mark_context_reserve_blocked")
    for lang in ("ru", "en"):
        assert "context_reserve" not in config.STRINGS[lang]


@pytest.mark.asyncio
async def test_t3_manual_compact_remains_explicit_operator_recovery():
    runtime = Runtime(usage(400_000))
    compact = CompactDriver(runtime, after_usage=usage(40_000))
    asked: list[str] = []

    async def ask(_message, prompt, _chat_id):
        asked.append(prompt)

    state = make_state(runtime, compact, ask)

    assert await state.request_compact(automatic=False)
    assert compact.calls == 1
    assert asked == []
