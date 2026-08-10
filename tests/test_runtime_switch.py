"""Runtime switching (#16 T5) — phase gate, fallback, handoff.

The hard requirement behind these tests: switching must never damage the
working Claude path. A refused switch is fine; a half-switched chat is not.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_state import ChatPhase, ChatState, PendingEntry  # noqa: E402


class FakeSession:
    """Minimal ChatRuntime stand-in."""

    def __init__(self, name="claude", model="m", probe_ok=True):
        self.name = name
        self.model = model
        self.session_id = f"sid-{name}"
        self.rate_limit = None
        self.usage_limit_active = False
        self.disconnected = False
        self.interrupted = False
        self.reconnects = 0
        self.received: list[str] = []
        self.injected: list[str] = []
        self._probe_ok = probe_ok

    async def read_quota(self):
        if not self._probe_ok:
            raise RuntimeError("app-server did not answer")
        self.rate_limit = {"primary": {"usedPercent": 42, "resetsAt": 1786168425},
                           "planType": "prolite"}
        return self.rate_limit

    def quota_summary(self):
        if not self.rate_limit:
            return None
        return {"used_percent": 42, "resets_at": 1786168425,
                "resets_human": "08.08 12:53", "plan": "prolite"}

    async def send_message(self, text):
        self.received.append(text)
        yield {"type": "text_delta", "content": "ok"}
        yield {"type": "turn_done"}

    async def inject_context(self, text):
        self.injected.append(text)

    async def check_context_reserve(self, combined="", *, manual=False):
        return {"ok": True, "reason": None}

    async def get_context_usage(self, *, refresh=False, preserve_session=False):
        return None

    async def interrupt(self):
        self.interrupted = True

    def reconnect(self):
        self.reconnects += 1

    async def reset_async(self):
        pass

    async def safe_disconnect(self):
        self.disconnected = True


class FakeActivityStore:
    def begin_activity(self, chat_id, now_utc=None):
        return ""

    def finish_activity(self, chat_id, now_utc=None):
        return ""

    def get_activity(self, chat_id):
        return None

    def claim_auto_attempt(self, chat_id, last_activity_utc):
        return False


def make_chat(tmp_path, monkeypatch, *, new_session=None, history=None):
    """A ChatState wired with fakes, plus the sessions it can switch between."""
    built: dict[str, FakeSession] = {}
    current = FakeSession("claude")

    def build(runtime_id, chat_id):
        session = new_session if new_session is not None else FakeSession(runtime_id)
        built[runtime_id] = session
        return session

    chat = ChatState(
        chat_id=42,
        session=current,
        bot=None,
        debounce_sec=1,
        ask_fn=None,
        set_current_chat_fn=lambda cid: None,
        get_lazy_block_fn=lambda cid: ("", [], []),
        compact_session_fn=None,
        activity_store=FakeActivityStore(),
        work_dir=str(tmp_path),
        runtime_id="claude",
        build_runtime_fn=build,
    )

    class FakeDB:
        def get_history(self, chat_id, limit=50, offset=0):
            return history or []

    monkeypatch.setattr("message_log.get_db", lambda: FakeDB())
    return chat, built


# ---------- phase gate (T5b) ----------


@pytest.mark.parametrize("phase", [
    ChatPhase.PROCESSING, ChatPhase.STOPPING, ChatPhase.COMPACTING,
])
def test_switch_refused_while_a_turn_is_active(tmp_path, monkeypatch, phase):
    """Tearing down a live turn is exactly how the working bot breaks."""
    chat, built = make_chat(tmp_path, monkeypatch)
    chat.phase = phase
    original = chat.session

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert result["reason"] == "busy"
    assert chat.session is original, "session swapped during an active turn"
    assert chat.runtime_id == "claude"
    assert not built, "a backend was built despite the refusal"
    assert chat.phase is phase, "phase was disturbed by a refused switch"


def test_switch_succeeds_from_idle(tmp_path, monkeypatch):
    chat, built = make_chat(tmp_path, monkeypatch)
    old = chat.session

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert chat.runtime_id == "codex"
    assert chat.session is built["codex"]
    assert old.disconnected, "old runtime was left connected"
    assert chat.phase is ChatPhase.IDLE


def test_switch_to_the_same_runtime_is_a_noop(tmp_path, monkeypatch):
    chat, built = make_chat(tmp_path, monkeypatch)
    original = chat.session

    result = asyncio.run(chat.switch_runtime("claude"))

    assert result == {"ok": False, "reason": "same", "runtime": "claude"}
    assert chat.session is original
    assert not built


def test_unknown_runtime_is_rejected_before_anything_is_touched(tmp_path, monkeypatch):
    chat, built = make_chat(tmp_path, monkeypatch)
    original = chat.session

    result = asyncio.run(chat.switch_runtime("gemini"))

    assert result["ok"] is False
    assert result["reason"] == "unknown"
    assert chat.session is original
    assert not built


# ---------- fallback (T5d) ----------


def test_failed_probe_keeps_the_old_runtime_alive(tmp_path, monkeypatch):
    """A runtime whose process starts but cannot answer must not be adopted.

    The user keeps a working bot; the failure is reported, never silent.
    """
    broken = FakeSession("codex", probe_ok=False)
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=broken)
    old = chat.session

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert result["reason"] == "unavailable"
    assert result["fallback"] == "claude"
    assert result["error"], "failure must carry a reason for the user"
    assert chat.session is old, "fell back to a broken runtime"
    assert chat.runtime_id == "claude"
    assert not old.disconnected, "incumbent was disconnected despite the failure"
    assert broken.disconnected, "the failed candidate was left running"
    assert chat.phase is ChatPhase.IDLE, "chat left stuck in a non-idle phase"


def test_old_runtime_is_probed_before_the_incumbent_is_dropped(tmp_path, monkeypatch):
    """Ordering guard: the probe must run while the old session is still live."""
    order: list[str] = []
    old = FakeSession("claude")
    new = FakeSession("codex")

    async def probe():
        order.append(f"probe(old_disconnected={old.disconnected})")
        return {"primary": {"usedPercent": 1}}

    async def disconnect():
        order.append("old_disconnect")
        old.disconnected = True

    new.read_quota = probe
    old.safe_disconnect = disconnect

    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new)
    chat.session = old

    asyncio.run(chat.switch_runtime("codex"))

    assert order == ["probe(old_disconnected=False)", "old_disconnect"]


# ---------- handoff (T5c) ----------


def test_handoff_is_delivered_as_user_text_with_a_disclaimer(tmp_path, monkeypatch):
    history = [
        {"role": "assistant", "content": "Привет!"},
        {"role": "user", "content": "Как дела?"},
    ]
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new, history=history)

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert len(new.injected) == 1, "handoff must be delivered exactly once"
    assert new.received == [], "handoff opened an agentic turn"
    sent = new.injected[0]
    assert "Переключение рантайма" in sent, "missing disclaimer"
    assert "Как дела?" in sent and "Привет!" in sent
    # Oldest first, so the new runtime reads the conversation in order.
    assert sent.index("Как дела?") < sent.index("Привет!")


def test_empty_history_does_not_block_the_switch(tmp_path, monkeypatch):
    """An emergency switch must not fail because there is nothing to carry."""
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new, history=[])

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert result["handoff"] is None
    assert result["handoff_status"] == "empty"
    assert new.injected == []


def test_unavailable_history_does_not_block_the_switch(tmp_path, monkeypatch):
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new)

    class Broken:
        def get_history(self, *a, **kw):
            raise RuntimeError("db is gone")

    monkeypatch.setattr("message_log.get_db", lambda: Broken())

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert result["handoff"] is None


def test_failed_handoff_delivery_discards_candidate(tmp_path, monkeypatch):
    """An indeterminate candidate is discarded instead of adopted."""
    new = FakeSession("codex")

    async def broken_inject(text):
        raise RuntimeError("turn failed")

    new.inject_context = broken_inject
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new,
                        history=[{"role": "user", "content": "hi"}])

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert chat.runtime_id == "claude"
    assert new.interrupted and new.disconnected
    assert chat.phase is ChatPhase.IDLE


def test_handoff_respects_its_character_budget(tmp_path, monkeypatch):
    from chat_state import HANDOFF_MAX_CHARS

    history = [{"role": "user", "content": "x" * 5_000} for _ in range(50)]
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new, history=history)

    asyncio.run(chat.switch_runtime("codex"))

    assert len(new.injected[0]) < HANDOFF_MAX_CHARS + 2_000


def test_agentic_only_runtime_skips_handoff_instead_of_running_it(
    tmp_path, monkeypatch
):
    """Claude has no passive ingress; old tasks must never be sent as a turn."""
    new = FakeSession("claude")
    chat, _ = make_chat(
        tmp_path,
        monkeypatch,
        new_session=new,
        history=[{"role": "user", "content": "send every email"}],
    )
    chat.runtime_id = "codex"

    result = asyncio.run(chat.switch_runtime("claude"))

    assert result["ok"] is True
    assert result["handoff"] is None
    assert result["handoff_status"] == "unsupported"
    assert new.received == []
    assert new.injected == []


def test_handoff_timeout_discards_candidate_and_drains_once(
    tmp_path, monkeypatch
):
    """Mutation guard: removing wait_for used to latch PROCESSING forever."""
    new = FakeSession("codex")
    chat, _ = make_chat(
        tmp_path,
        monkeypatch,
        new_session=new,
        history=[{"role": "user", "content": "old task"}],
    )
    processed: list[list[PendingEntry]] = []

    async def record_batch(batch):
        processed.append(batch)
        async with chat._lock:
            chat.phase = ChatPhase.IDLE

    async def hanging_inject(text):
        await chat.run_urgent_prompt("queued once")
        await asyncio.sleep(0.2)

    chat._start_processing = record_batch
    new.inject_context = hanging_inject
    monkeypatch.setattr("chat_state.HANDOFF_TIMEOUT_SECONDS", 0.01)

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert chat.runtime_id == "claude"
    assert new.interrupted and new.disconnected
    assert chat.phase is ChatPhase.IDLE
    assert [e.prompt for batch in processed for e in batch] == ["queued once"]
    assert chat.deferred == []


def test_cancelled_handoff_cleans_candidate_and_drains_once(tmp_path, monkeypatch):
    """Cancellation is a lifecycle path, not permission to leak a process."""
    new = FakeSession("codex")
    chat, _ = make_chat(
        tmp_path,
        monkeypatch,
        new_session=new,
        history=[{"role": "user", "content": "old task"}],
    )
    entered = asyncio.Event()
    processed: list[list[PendingEntry]] = []

    async def record_batch(batch):
        processed.append(batch)
        async with chat._lock:
            chat.phase = ChatPhase.IDLE

    async def hanging_inject(text):
        entered.set()
        await asyncio.Future()

    chat._start_processing = record_batch
    new.inject_context = hanging_inject

    async def scenario():
        task = asyncio.create_task(chat.switch_runtime("codex"))
        await entered.wait()
        await chat.run_urgent_prompt("queued once")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert chat.runtime_id == "claude"
    assert new.interrupted and new.disconnected
    assert chat.phase is ChatPhase.IDLE
    assert [e.prompt for batch in processed for e in batch] == ["queued once"]
    assert chat.deferred == []


def test_candidate_disconnect_timeout_forces_process_reconnect(tmp_path, monkeypatch):
    """A broken disconnect must not turn bounded handoff cleanup into a leak."""
    new = FakeSession("codex")
    chat, _ = make_chat(
        tmp_path,
        monkeypatch,
        new_session=new,
        history=[{"role": "user", "content": "old task"}],
    )

    async def broken_inject(text):
        raise RuntimeError("inject failed")

    async def hanging_disconnect():
        await asyncio.sleep(0.2)

    new.inject_context = broken_inject
    new.safe_disconnect = hanging_disconnect
    monkeypatch.setattr("chat_state.SWITCH_CLEANUP_TIMEOUT_SECONDS", 0.01)

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert new.interrupted is True
    assert new.reconnects == 1
    assert chat.phase is ChatPhase.IDLE


def test_cancel_after_adoption_still_retires_old_and_drains_once(
    tmp_path, monkeypatch
):
    """Mutation guard: cancellation is safe on both sides of session adoption."""
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new, history=[])
    old = chat.session
    entered = asyncio.Event()
    processed: list[list[PendingEntry]] = []

    async def hanging_old_disconnect():
        entered.set()
        await asyncio.sleep(0.2)

    async def record_batch(batch):
        processed.append(batch)
        async with chat._lock:
            chat.phase = ChatPhase.IDLE

    old.safe_disconnect = hanging_old_disconnect
    chat._start_processing = record_batch
    monkeypatch.setattr("chat_state.SWITCH_CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(chat.switch_runtime("codex"))
        await entered.wait()
        await chat.run_urgent_prompt("queued once")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert chat.runtime_id == "codex"
    assert chat.session is new
    assert old.reconnects == 1
    assert chat.phase is ChatPhase.IDLE
    assert [e.prompt for batch in processed for e in batch] == ["queued once"]
    assert chat.deferred == []


# ---------- work arriving during a switch (T5b, reminders) ----------


def test_an_urgent_reminder_firing_mid_switch_is_delivered_exactly_once(
    tmp_path, monkeypatch
):
    """The real question the user asked: do reminders survive a switch?

    Drives the actual `run_urgent_prompt` while the swap is in flight and
    measures what reaches processing — not what the code is supposed to do.
    A reminder must arrive exactly once: losing it is a missed alarm,
    duplicating it is the bot nagging twice.
    """
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new)

    processed: list[list[PendingEntry]] = []

    async def record_batch(batch):
        processed.append(batch)
        async with chat._lock:
            chat.phase = ChatPhase.IDLE

    chat._start_processing = record_batch

    async def probe_then_remind():
        # The reminder fires at the worst possible moment: mid-switch.
        await chat.run_urgent_prompt("прими таблетки")
        return {"primary": {"usedPercent": 1}}

    new.read_quota = probe_then_remind

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True, "switch failed"
    delivered = [e.prompt for batch in processed for e in batch]
    assert delivered == ["прими таблетки"], (
        f"reminder not delivered exactly once: {delivered}"
    )
    assert chat.deferred == [], "reminder left stranded in the deferred queue"


def test_a_reminder_is_not_lost_when_the_switch_fails(tmp_path, monkeypatch):
    """Even on the failure path the deferred work must still be drained."""
    broken = FakeSession("codex", probe_ok=False)
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=broken)

    processed: list[list[PendingEntry]] = []

    async def record_batch(batch):
        processed.append(batch)
        async with chat._lock:
            chat.phase = ChatPhase.IDLE

    chat._start_processing = record_batch

    async def fail_after_reminder():
        await chat.run_urgent_prompt("прими таблетки")
        raise RuntimeError("app-server did not answer")

    broken.read_quota = fail_after_reminder

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    delivered = [e.prompt for batch in processed for e in batch]
    assert delivered == ["прими таблетки"], (
        f"reminder lost on the failure path: {delivered}"
    )


# ---------- bridge session revocation (T5e) ----------


def test_switch_revokes_the_old_runtimes_bridge_handles(tmp_path, monkeypatch):
    """A handle must not outlive the runtime it was issued to."""
    import tool_bridge

    tool_bridge._SESSIONS.clear()
    stale = tool_bridge.issue_session(42, runtime="claude")
    chat, _ = make_chat(tmp_path, monkeypatch)

    assert tool_bridge.session_chat(stale) == 42
    asyncio.run(chat.switch_runtime("codex"))
    assert tool_bridge.session_chat(stale) is None, "retired runtime kept a live handle"


def test_revocation_does_not_disturb_another_chat(tmp_path, monkeypatch):
    """With two users, one person's switch must not cancel the other's turn."""
    import tool_bridge

    tool_bridge._SESSIONS.clear()
    ours = tool_bridge.issue_session(42, runtime="claude")
    theirs = tool_bridge.issue_session(999, runtime="claude")
    chat, _ = make_chat(tmp_path, monkeypatch)

    asyncio.run(chat.switch_runtime("codex"))

    assert tool_bridge.session_chat(ours) is None
    assert tool_bridge.session_chat(theirs) == 999, "another chat's session was revoked"


def test_revocation_is_scoped_to_the_retired_runtime(tmp_path, monkeypatch):
    """A handle already issued to the incoming runtime must survive the switch."""
    import tool_bridge

    tool_bridge._SESSIONS.clear()
    old_handle = tool_bridge.issue_session(42, runtime="claude")
    new_handle = tool_bridge.issue_session(42, runtime="codex")
    chat, _ = make_chat(tmp_path, monkeypatch)

    asyncio.run(chat.switch_runtime("codex"))

    assert tool_bridge.session_chat(old_handle) is None
    assert tool_bridge.session_chat(new_handle) == 42


def test_revoke_chat_sessions_reports_what_it_removed(tmp_path):
    import tool_bridge

    tool_bridge._SESSIONS.clear()
    tool_bridge.issue_session(42, runtime="claude")
    tool_bridge.issue_session(42, runtime="claude")
    tool_bridge.issue_session(42, runtime="codex")

    assert tool_bridge.revoke_chat_sessions(42, "claude") == 2
    assert tool_bridge.revoke_chat_sessions(42) == 1
    assert tool_bridge.revoke_chat_sessions(42) == 0


def test_switch_survives_a_broken_bridge(tmp_path, monkeypatch):
    """Revocation is hygiene; failing it must not strand the chat mid-switch."""
    import tool_bridge

    def boom(*a, **kw):
        raise RuntimeError("bridge is down")

    monkeypatch.setattr(tool_bridge, "revoke_chat_sessions", boom)
    chat, _ = make_chat(tmp_path, monkeypatch)

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert chat.runtime_id == "codex"


def test_switch_without_a_builder_fails_safely(tmp_path, monkeypatch):
    """A chat constructed the old way must refuse, not crash mid-swap."""
    chat, _ = make_chat(tmp_path, monkeypatch)
    chat._build_runtime = None
    original = chat.session

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert chat.session is original
    assert chat.phase is ChatPhase.IDLE
