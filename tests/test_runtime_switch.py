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
        self.received: list[str] = []
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

    async def check_context_reserve(self, combined="", *, manual=False):
        return {"ok": True, "reason": None}

    async def get_context_usage(self, *, refresh=False, preserve_session=False):
        return None

    async def interrupt(self):
        pass

    def reconnect(self):
        pass

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
    assert len(new.received) == 1, "handoff must be delivered exactly once"
    sent = new.received[0]
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
    assert new.received == []


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


def test_failed_handoff_delivery_still_switches(tmp_path, monkeypatch):
    """The summary is a nicety; losing it must not cost the switch."""
    new = FakeSession("codex")

    async def broken_send(text):
        raise RuntimeError("turn failed")
        yield  # pragma: no cover

    new.send_message = broken_send
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new,
                        history=[{"role": "user", "content": "hi"}])

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert chat.runtime_id == "codex"


def test_handoff_respects_its_character_budget(tmp_path, monkeypatch):
    from chat_state import HANDOFF_MAX_CHARS

    history = [{"role": "user", "content": "x" * 5_000} for _ in range(50)]
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new, history=history)

    asyncio.run(chat.switch_runtime("codex"))

    assert len(new.received[0]) < HANDOFF_MAX_CHARS + 2_000


# ---------- work arriving during a switch (T5b, reminders) ----------


def test_a_reminder_arriving_mid_switch_is_not_lost(tmp_path, monkeypatch):
    """A reminder firing exactly during the swap must be deferred, not dropped.

    While switching, the chat is held in PROCESSING, so accept_entry defers the
    entry instead of racing the swap; the drain afterwards picks it up.
    """
    new = FakeSession("codex")
    chat, _ = make_chat(tmp_path, monkeypatch, new_session=new)
    drained: list[list[PendingEntry]] = []

    async def capture_drain(record_activity=True):
        drained.extend(chat.deferred)
        chat.deferred.clear()
        chat.phase = ChatPhase.IDLE

    async def slow_probe():
        # The reminder lands while the switch is in flight.
        await chat.accept_entry(PendingEntry(
            prompt="напоминание", message_id=0, message=None,
            source="reminder", reply_target=42,
        ))
        return {"primary": {"usedPercent": 1}}

    new.read_quota = slow_probe
    chat._drain_or_idle = capture_drain

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is True
    assert len(drained) == 1, "the reminder was lost during the switch"
    assert drained[0][0].prompt == "напоминание"


def test_switch_without_a_builder_fails_safely(tmp_path, monkeypatch):
    """A chat constructed the old way must refuse, not crash mid-swap."""
    chat, _ = make_chat(tmp_path, monkeypatch)
    chat._build_runtime = None
    original = chat.session

    result = asyncio.run(chat.switch_runtime("codex"))

    assert result["ok"] is False
    assert chat.session is original
    assert chat.phase is ChatPhase.IDLE
