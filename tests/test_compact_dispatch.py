"""Compact dispatch per runtime (#16 T6).

Kesha's own compaction is a Claude-only transaction: it asks the model for a
summary, screens that text, and atomically swaps the session. Codex compacts
internally and returns no summary to screen, so it gets its own path.

The Claude path must stay untouched — it survived a production incident and
carries the night window, the context reserve and the latch.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_state import ChatPhase, ChatState  # noqa: E402
from runtime_protocol import RuntimeCapabilities  # noqa: E402


class Caps:
    """Backends declare capabilities on the CLASS, as the registry requires."""

    def __init__(self, native):
        self.CAPABILITIES = RuntimeCapabilities(
            mid_turn_inject=False,
            native_compact=native,
            context_percentage=not native,
            cost_reporting=False,
            resume_across_restart=True,
        )


def make_session_class(native: bool, *, compact_raises=None, pct=50.0):
    class Session:
        CAPABILITIES = RuntimeCapabilities(
            mid_turn_inject=False,
            native_compact=native,
            context_percentage=not native,
            cost_reporting=False,
            resume_across_restart=True,
        )

        def __init__(self):
            self.model = "m"
            self.session_id = "sid"
            self.rate_limit = None
            self.usage_limit_active = False
            self.native_compacted = 0

        async def compact_context(self):
            if compact_raises:
                raise compact_raises
            self.native_compacted += 1
            return {"context_tokens": 4_000, "max_tokens": 258_400}

        async def get_context_usage(self, *, refresh=False, preserve_session=False):
            return {"percentage": pct, "totalTokens": 1000, "maxTokens": 2000}

        async def check_context_reserve(self, combined="", *, manual=False):
            return {"ok": True, "reason": None}

        async def interrupt(self):
            pass

        def reconnect(self):
            pass

        async def reset_async(self):
            pass

        async def safe_disconnect(self):
            pass

        async def send_message(self, text):
            yield {"type": "turn_done"}

    return Session


class FakeStore:
    def begin_activity(self, chat_id, now_utc=None):
        return ""

    def finish_activity(self, chat_id, now_utc=None):
        return ""

    def get_activity(self, chat_id):
        return None

    def claim_auto_attempt(self, chat_id, last):
        return False


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()

    async def edit_message_text(self, text, **kw):
        self.sent.append(text)
        return None


def make_chat(tmp_path, session, *, custom_compact=None, runtime_id="claude"):
    calls: list = []

    # `recent_rows` is the #21 verbatim tail. The stub must mirror the real
    # compact_session signature, otherwise it fails on the call itself and the
    # dispatch assertion below never gets to run.
    async def custom(sess, notify=None, recent_rows=None):
        calls.append(sess)
        custom.recent_rows_seen.append(recent_rows)
        if custom_compact:
            return await custom_compact(sess, notify)
        return {"ok": True, "before_pct": 50, "after_pct": 4}

    custom.recent_rows_seen = []

    chat = ChatState(
        chat_id=42,
        session=session,
        bot=FakeBot(),
        debounce_sec=1,
        ask_fn=None,
        set_current_chat_fn=lambda c: None,
        get_lazy_block_fn=lambda c: ("", [], []),
        compact_session_fn=custom,
        activity_store=FakeStore(),
        work_dir=str(tmp_path),
        runtime_id=runtime_id,
        build_runtime_fn=None,
    )
    return chat, calls


# ---------- dispatch ----------


def test_claude_still_uses_keshas_own_compaction(tmp_path):
    """The Claude path must be reached exactly as before."""
    session = make_session_class(native=False)()
    chat, calls = make_chat(tmp_path, session)

    asyncio.run(chat._do_compact(automatic=True))

    assert calls == [session], "Claude did not go through compact_session"
    assert session.native_compacted == 0
    # #21: the Claude path must also hand over the verbatim tail argument.
    assert chat._compact_session_fn.recent_rows_seen == [[]]


def test_codex_uses_its_own_native_compaction(tmp_path):
    session = make_session_class(native=True)()
    chat, calls = make_chat(tmp_path, session, runtime_id="codex")

    asyncio.run(chat._do_compact(automatic=True))

    assert session.native_compacted == 1
    assert calls == [], "native runtime must not run Kesha's transaction"


def test_dispatch_reads_declared_capabilities_not_the_runtime_name(tmp_path):
    """A runtime called anything must be routed by what it declares."""
    session = make_session_class(native=True)()
    chat, calls = make_chat(tmp_path, session, runtime_id="claude")  # lying name

    asyncio.run(chat._do_compact(automatic=True))

    assert session.native_compacted == 1
    assert calls == []


def test_native_compact_reports_the_result_to_the_user(tmp_path):
    session = make_session_class(native=True)()
    chat, _ = make_chat(tmp_path, session, runtime_id="codex")

    asyncio.run(chat._do_compact(automatic=False))

    text = " ".join(chat.bot.sent)
    assert "codex" in text, "user is not told whose compaction ran"
    assert "✅" in text


def test_native_compact_does_not_repeat_an_unmeasured_percentage(tmp_path):
    session = make_session_class(native=True, pct=91.4)()
    chat, _ = make_chat(tmp_path, session, runtime_id="codex")

    asyncio.run(chat._do_compact(automatic=False))

    text = " ".join(chat.bot.sent)
    assert "91% → 91%" not in text
    assert "следующего сообщения" in text


def test_failed_native_compact_is_reported_not_swallowed(tmp_path):
    session = make_session_class(
        native=True, compact_raises=RuntimeError("app-server exited")
    )()
    chat, _ = make_chat(tmp_path, session, runtime_id="codex")

    asyncio.run(chat._do_compact(automatic=False))

    text = " ".join(chat.bot.sent)
    assert "app-server exited" in text
    assert chat.phase is ChatPhase.IDLE, "chat left stuck after a failed compact"


def test_a_failed_compact_still_releases_the_chat(tmp_path):
    session = make_session_class(
        native=True, compact_raises=RuntimeError("boom")
    )()
    chat, _ = make_chat(tmp_path, session, runtime_id="codex")
    chat.compact_requested = True

    asyncio.run(chat._do_compact(automatic=True))

    assert chat.compact_requested is False
    assert chat._compact_started is False


def test_cancelled_native_compact_releases_the_chat(tmp_path):
    started = asyncio.Event()

    class Session(make_session_class(native=True)):
        async def compact_context(self):
            started.set()
            await asyncio.Event().wait()

    session = Session()
    chat, _ = make_chat(tmp_path, session, runtime_id="codex")
    chat.phase = ChatPhase.COMPACTING
    chat.compact_requested = True
    chat._compact_started = True

    async def scenario():
        task = asyncio.create_task(chat._do_compact(automatic=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert chat.phase is ChatPhase.IDLE
    assert chat.compact_requested is False
    assert chat._compact_started is False


def test_manual_compact_still_works_on_a_native_runtime(tmp_path):
    """Disabling the timer must not disable the user's own /compact."""
    session = make_session_class(native=True)()
    chat, _ = make_chat(tmp_path, session, runtime_id="codex")

    asyncio.run(chat._do_compact(automatic=False))

    assert session.native_compacted == 1


# ---------- capability property ----------


@pytest.mark.parametrize("native", [True, False])
def test_native_compact_property_follows_the_backend(tmp_path, native):
    session = make_session_class(native=native)()
    chat, _ = make_chat(tmp_path, session)
    assert chat._native_compact is native


def test_backend_without_capabilities_defaults_to_keshas_compaction(tmp_path):
    """An unknown backend must not silently skip compaction entirely."""

    class Bare:
        model = "m"
        session_id = "sid"
        rate_limit = None
        usage_limit_active = False

        async def get_context_usage(self, **kw):
            return None

        async def check_context_reserve(self, combined="", *, manual=False):
            return {"ok": True}

        async def interrupt(self):
            pass

        def reconnect(self):
            pass

        async def reset_async(self):
            pass

        async def safe_disconnect(self):
            pass

    chat, calls = make_chat(tmp_path, Bare())
    assert chat._native_compact is False

    asyncio.run(chat._do_compact(automatic=True))
    assert len(calls) == 1
