"""Quota exhaustion per runtime (#16 T7) — mocks only, no live Codex calls.

The user spent an hour looking at "try later" without knowing which
subscription was out or when it returns. A terminal limit must therefore say
BOTH: whose limit, and when it resets.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import response_stream  # noqa: E402
from config import STRINGS, render  # noqa: E402

RAW = "You've hit your usage limit"


class LimitedCodexSession:
    """Codex runtime that dies of quota partway through an answer."""

    def __init__(self, *, quota=True, streamed="Сейчас посчитаю"):
        self._quota = quota
        self._streamed = streamed
        self.model = "gpt-5.6-sol"
        self.session_id = "codex-thread"
        self.usage_limit_active = False

    def quota_summary(self):
        if not self._quota:
            return None
        return {
            "used_percent": 100,
            "resets_at": 1786168425,
            "resets_human": "08.08 12:53",
            "plan": "prolite",
        }

    async def send_message(self, prompt):
        # The user has already seen text appear before the quota dies.
        yield {"type": "text_delta", "content": self._streamed}
        yield {"type": "error", "kind": "usage_limit", "content": RAW}


class FakeState:
    def __init__(self, session, runtime_id="codex"):
        self.session = session
        self.runtime_id = runtime_id
        self.reserve_blocked = False

    def should_stop(self):
        return False

    async def mark_context_reserve_blocked(self):
        self.reserve_blocked = True


class FakeRegistry:
    def __init__(self, session, runtime_id="codex"):
        self.state = FakeState(session, runtime_id)

    def get(self, chat_id):
        return self.state


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": len(self.sent)})()

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return None

    async def delete_message(self, chat_id, message_id):
        return None

    async def send_chat_action(self, *a, **kw):
        return None


async def completed_typer():
    return None


def visible_text(bot) -> str:
    return " ".join([t for _, t in bot.sent] + [t for t, _ in bot.edits])


async def run_turn(session, runtime_id="codex"):
    bot = FakeBot()
    response_stream.set_bot(bot)
    response_stream.set_registry(FakeRegistry(session, runtime_id))
    typer = asyncio.create_task(completed_typer())
    await typer
    await response_stream._ask_inner(None, "посчитай что-нибудь", 7, typer)
    return bot


# ---------- the scenario that will happen on prod at 98% quota ----------


@pytest.mark.asyncio
async def test_quota_dying_mid_turn_tells_the_user_why_and_until_when():
    """Mid-answer quota death must not look like the bot going silent."""
    bot = await run_turn(LimitedCodexSession())
    text = visible_text(bot)

    assert "лимит" in text.lower(), "user is not told there is a limit"
    assert "08.08 12:53" in text, "the real reset time was dropped"
    assert RAW not in text, "raw provider error leaked to the user"


@pytest.mark.asyncio
async def test_the_limit_message_names_the_runtime_that_is_out():
    """Saying 'Claude' while running on Codex sends the user to the wrong account."""
    bot = await run_turn(LimitedCodexSession(), runtime_id="codex")
    text = visible_text(bot)

    assert "codex" in text.lower()
    assert "claude" not in text.lower(), "named the wrong subscription"


@pytest.mark.asyncio
async def test_claude_limit_still_names_claude():
    """The default path must not regress into saying 'codex'."""

    class LimitedClaude(LimitedCodexSession):
        def __init__(self):
            super().__init__()
            self.model = "claude-opus-5"

        def quota_summary(self):  # Claude reports no structured quota
            return None

    bot = await run_turn(LimitedClaude(), runtime_id="claude")
    text = visible_text(bot)
    assert "claude" in text.lower()


@pytest.mark.asyncio
async def test_a_runtime_without_quota_data_still_produces_a_clear_message():
    """No reset date is not an excuse for an empty or raw message."""
    bot = await run_turn(LimitedCodexSession(quota=False))
    text = visible_text(bot)

    assert "лимит" in text.lower()
    assert RAW not in text
    # It must not fabricate a date it does not have. ("жду сброса" is the
    # ordinary wording; what must be absent is a parenthesised timestamp.)
    assert "(сброс" not in text.lower()


@pytest.mark.asyncio
async def test_the_live_bubble_becomes_the_limit_notice():
    """The streamed bubble must end as the explanation, not as a cut-off answer.

    NOTE: on the reminder path a partial answer already flushed as a SEPARATE
    message survives (verified against main — pre-existing, not introduced
    here). The guarantee tested is that the live bubble itself is replaced, so
    the last thing the user sees is the reason and not a dangling half-sentence.
    """
    bot = await run_turn(LimitedCodexSession(streamed="Сейчас посчитаю, значит"))

    assert bot.edits, "the live bubble was never finalized"
    final = bot.edits[-1][0]
    assert "лимит" in final.lower()
    assert "Сейчас посчитаю, значит" not in final


@pytest.mark.asyncio
async def test_limit_is_terminal_and_not_retried():
    """Retrying an exhausted quota burns nothing but time; it must stop at once."""
    calls = []

    class Counting(LimitedCodexSession):
        async def send_message(self, prompt):
            calls.append(prompt)
            yield {"type": "text_delta", "content": "x"}
            yield {"type": "error", "kind": "usage_limit", "content": RAW}

    await run_turn(Counting())
    assert len(calls) == 1, f"limit was retried {len(calls)} times"


# ---------- #6: the limit notice carries the real windows ----------


@pytest.fixture
def claude_windows(monkeypatch):
    """Claude's oauth/usage payload, frozen — no network, no wall clock."""
    import quota

    now = datetime.now(timezone.utc)

    async def fake_fetch():
        return {
            "five_hour": {"utilization": 99.0,
                          "resets_at": (now + timedelta(minutes=18)).isoformat()},
            "seven_day": {"utilization": 15.0,
                          "resets_at": (now + timedelta(days=6)).isoformat()},
        }

    monkeypatch.setattr(quota, "fetch_claude_usage", fake_fetch)


@pytest.mark.asyncio
async def test_the_stream_limit_notice_shows_the_windows(claude_windows):
    """A bare "wait for the reset" is what this ticket exists to remove."""

    class LimitedClaude(LimitedCodexSession):
        def quota_summary(self):
            return None

    text = visible_text(await run_turn(LimitedClaude(), runtime_id="claude"))

    assert "5h: 99%" in text, f"5h window missing: {text!r}"
    assert "7d: 15%" in text, f"7d window missing: {text!r}"


@pytest.mark.asyncio
async def test_the_reserve_limit_notice_shows_the_windows(claude_windows):
    """The pre-turn rejection path renders the same block."""

    class Refusing(LimitedCodexSession):
        def quota_summary(self):
            return None

        async def check_context_reserve(self, combined="", *, manual=False):
            return {"ok": False, "reason": "usage_limit"}

    chat = _chat_with(Refusing(), runtime_id="claude")
    chat._activity_store.finish_activity = lambda *a, **kw: ""
    await chat._run_batch([_entry()])

    text = " ".join(t for _, t in chat.bot.sent)
    assert "5h: 99%" in text, f"5h window missing: {text!r}"


@pytest.mark.asyncio
async def test_no_quota_data_leaves_the_old_message_untouched():
    """Absent windows must not leave a dangling blank line or placeholder."""
    bot = await run_turn(LimitedCodexSession(quota=False))
    final = bot.edits[-1][0]

    assert "5h:" not in final and "{quota}" not in final
    assert final == final.strip(), f"notice ends with a dangling blank line: {final!r}"


@pytest.mark.asyncio
async def test_the_notice_survives_a_registry_that_dies_at_the_limit(claude_windows):
    """Nothing gathered for the decoration may cost the notice itself.

    The session lookup is only reachable here because the limit already
    happened — if it fails now, the user must still learn why the turn died.
    """
    state_box = {}

    class Dying(LimitedCodexSession):
        def quota_summary(self):
            return None

        async def send_message(self, prompt):
            yield {"type": "text_delta", "content": "считаю"}
            state_box["state"].dead = True
            yield {"type": "error", "kind": "usage_limit", "content": RAW}

    class DyingState:
        dead = False

        def __init__(self, session, runtime_id):
            self._session = session
            self.runtime_id = runtime_id

        @property
        def session(self):
            if self.dead:
                raise RuntimeError("session lookup gone")
            return self._session

        def should_stop(self):
            return False

    class DyingRegistry(FakeRegistry):
        def __init__(self, session, runtime_id):
            self.state = DyingState(session, runtime_id)

        def get(self, chat_id):
            return self.state

    bot = FakeBot()
    registry = DyingRegistry(Dying(), "claude")
    state_box["state"] = registry.state
    response_stream.set_bot(bot)
    response_stream.set_registry(registry)
    typer = asyncio.create_task(completed_typer())
    await typer
    await response_stream._ask_inner(None, "посчитай", 7, typer)

    assert "лимит" in visible_text(bot).lower(), "the explanation was lost"


def test_a_message_without_a_sender_still_renders_a_notice():
    """Channel posts have from_user=None; the notice must not die on it."""
    from config import lang_of, render

    class NoSender:
        from_user = None

    assert lang_of(NoSender()) == "ru"
    assert "лимит" in render("session_limit", lang_of(NoSender()),
                             runtime="claude", reset="").lower()


# ---------- the helpers, directly ----------


def test_runtime_limit_suffix_prefers_the_runtimes_own_data():
    response_stream.set_registry(FakeRegistry(LimitedCodexSession()))
    assert response_stream._runtime_limit_suffix(7) == " (сброс 08.08 12:53)"


def test_runtime_limit_suffix_is_none_when_the_runtime_cannot_say():
    response_stream.set_registry(FakeRegistry(LimitedCodexSession(quota=False)))
    assert response_stream._runtime_limit_suffix(7) is None


def test_runtime_limit_suffix_survives_a_backend_without_quota_support():
    class Bare:
        pass

    response_stream.set_registry(FakeRegistry(Bare()))
    assert response_stream._runtime_limit_suffix(7) is None


def test_runtime_label_falls_back_when_the_registry_cannot_answer():
    class Broken:
        def get(self, chat_id):
            raise RuntimeError("no registry")

    response_stream.set_registry(Broken())
    assert response_stream._runtime_label(7) == ""


# ---------- the pre-turn reserve path (chat_state) ----------


def _chat_with(session, runtime_id="codex"):
    """A ChatState wired just enough to render a rejected batch."""
    from chat_state import ChatState

    class Store:
        def begin_activity(self, *a, **kw):
            return ""

        def finish_activity(self, *a, **kw):
            return ""

        def get_activity(self, chat_id):
            return None

        def claim_auto_attempt(self, *a):
            return False

    return ChatState(
        chat_id=42, session=session, bot=FakeBot(), debounce_sec=1, ask_fn=None,
        set_current_chat_fn=lambda c: None, get_lazy_block_fn=lambda c: ("", [], []),
        compact_session_fn=None, activity_store=Store(), work_dir="/tmp",
        runtime_id=runtime_id, build_runtime_fn=None,
    )


@pytest.mark.asyncio
async def test_reserve_rejection_names_the_runtime_and_its_reset():
    """The pre-turn reserve path had the same hardcoded 'Claude' as the stream.

    Drives the real rejection through _run_batch so that removing the
    `fmt = self._limit_fmt()` wiring fails here — asserting on the helper
    alone would leave the call site untested.
    """
    from chat_state import PendingEntry

    class Refusing(LimitedCodexSession):
        async def check_context_reserve(self, combined="", *, manual=False):
            return {"ok": False, "reason": "usage_limit"}

    chat = _chat_with(Refusing(), runtime_id="codex")
    chat._activity_store.finish_activity = lambda *a, **kw: ""
    await chat._run_batch([
        PendingEntry(prompt="привет", message_id=0, message=None,
                     source="reminder", reply_target=42)
    ])

    text = " ".join(t for _, t in chat.bot.sent)
    assert "codex" in text, f"runtime not named: {text!r}"
    assert "08.08 12:53" in text, f"reset time missing: {text!r}"
    assert "Claude" not in text, "named the wrong subscription"


@pytest.mark.asyncio
async def test_reserve_rejection_omits_a_reset_it_does_not_know():
    chat = _chat_with(LimitedCodexSession(quota=False), runtime_id="codex")
    fmt = await chat._limit_fmt()

    assert fmt["reset"] == ""
    rendered = render("context_usage_limit", **fmt)
    assert "(сброс" not in rendered


@pytest.mark.asyncio
async def test_reserve_rejection_still_names_claude_on_the_default_runtime():
    class Bare:
        pass

    chat = _chat_with(Bare(), runtime_id="claude")
    rendered = render("context_usage_limit", **await chat._limit_fmt())
    assert "claude" in rendered.lower()


def test_no_user_facing_string_hardcodes_a_provider_on_a_shared_path():
    """Guard against the next copy of this bug.

    Strings reachable from BOTH runtimes must not name one. compact.py's
    Claude-specific text is exempt: it is only reachable through the
    non-native compaction branch (verified in T6).
    """
    shared_keys = (
        "session_limit", "context_usage_limit", "context_limit",
        "context_reserve", "context_unknown", "session_unavailable",
        "compact_floor",
    )
    for lang in ("ru", "en"):
        for key in shared_keys:
            text = STRINGS[lang][key]
            assert "Claude" not in text, (
                f"STRINGS[{lang}][{key}] hardcodes a provider on a shared path"
            )


def test_a_terminal_notice_never_dies_on_a_missing_placeholder():
    """Adding a placeholder must not turn a notice into silence.

    Caught by an existing test when {runtime}/{reset} were introduced: a
    caller that omits them used to raise KeyError, which would swallow the
    very explanation the message exists to deliver.
    """
    from config import render

    for key in ("context_usage_limit", "session_limit"):
        for lang in ("ru", "en"):
            text = render(key, lang)          # no kwargs at all
            assert text, f"{lang}/{key} rendered empty"
            assert "{" not in text, f"{lang}/{key} leaked a raw placeholder"


def test_render_still_substitutes_what_it_is_given():
    from config import render

    text = render("context_usage_limit", "ru", runtime="codex",
                  reset=" (сброс 08.08 12:53)")
    assert "codex" in text and "08.08 12:53" in text


def test_limit_string_carries_both_runtime_and_reset():
    for lang in ("ru", "en"):
        rendered = render("session_limit", lang,
                          runtime="codex", reset=" (сброс 08.08 12:53)")
        assert "codex" in rendered
        assert "08.08 12:53" in rendered


# ---------- #25: one automatic retry on runtime_unhealthy ----------


class ProbeSession(LimitedCodexSession):
    """Records every reserve call and replays a scripted list of outcomes."""

    def __init__(self, outcomes):
        super().__init__(quota=False)
        self.outcomes = list(outcomes)
        self.calls = 0
        self.asked = []

    async def check_context_reserve(self, combined="", *, manual=False):
        self.calls += 1
        self.asked.append(combined)
        # Fail loud instead of hanging: an unbounded retry loop would spin here
        # forever and the test would time out rather than report a failure.
        if self.calls > len(self.outcomes) + 2:
            raise AssertionError(f"reserve probed {self.calls} times — retry is unbounded")
        return self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]


def _entry():
    from chat_state import PendingEntry

    return PendingEntry(
        prompt="привет", message_id=0, message=None,
        source="reminder", reply_target=42,
    )


@pytest.mark.asyncio
async def test_runtime_unhealthy_retries_once_and_succeeds_silently(monkeypatch):
    """A recovered runtime must cost the user nothing — not even a notice.

    Measured: after reconnect() drops the wedged client the retry builds a NEW
    CLI process and the reserve succeeds in 8-15s (docs/tasks/25 F2).
    """
    session = ProbeSession([
        {"ok": False, "reason": "runtime_unhealthy"},
        {"ok": True, "reason": None, "remaining": 900_000},
    ])
    chat = _chat_with(session, runtime_id="claude")
    chat._activity_store.finish_activity = lambda *a, **kw: ""
    asked = []

    async def ask(msg, combined, cid):
        asked.append(combined)

    chat._ask_fn = ask

    monkeypatch.setattr("message_log.get_db", lambda: type(
        "D", (), {"log_user": lambda *a, **kw: 0}
    )())

    await chat._run_batch([_entry()])

    assert session.calls == 2, "expected exactly one retry"
    assert chat.bot.sent == [], f"user was notified anyway: {chat.bot.sent}"
    assert asked, "recovered batch never reached the model"


@pytest.mark.asyncio
async def test_runtime_unhealthy_twice_refuses_once_and_stops():
    """A still-dead runtime: exactly two probes, exactly one notice."""
    session = ProbeSession([{"ok": False, "reason": "runtime_unhealthy"}])
    chat = _chat_with(session, runtime_id="claude")
    chat._activity_store.finish_activity = lambda *a, **kw: ""

    await chat._run_batch([_entry()])

    assert session.calls == 2, f"retry was not bounded: {session.calls} calls"
    assert len(chat.bot.sent) == 1, f"expected one notice: {chat.bot.sent}"


@pytest.mark.asyncio
async def test_retry_reports_the_reason_the_retry_actually_returned():
    """Fail-closed #14: if the retry measures a full context, say `reserve`."""
    from config import STRINGS

    session = ProbeSession([
        {"ok": False, "reason": "runtime_unhealthy"},
        {"ok": False, "reason": "reserve"},
    ])
    chat = _chat_with(session, runtime_id="claude")
    chat._activity_store.finish_activity = lambda *a, **kw: ""

    await chat._run_batch([_entry()])

    text = " ".join(t for _, t in chat.bot.sent)
    assert text == STRINGS["ru"]["context_reserve"], text
    assert chat._context_reserve_blocked is True, "reserve latch not set on retry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["reserve", "usage_limit", "runtime_invariant", "session_unavailable", "unknown"],
)
async def test_other_reasons_are_never_retried(reason):
    """Retrying a full context or an exhausted quota is forbidden.

    `reserve` would hammer a context we just measured as full (against #14);
    `usage_limit` would retry a quota error the project rule says to wait out.
    """
    session = ProbeSession([{"ok": False, "reason": reason}])
    chat = _chat_with(session, runtime_id="claude")
    chat._activity_store.finish_activity = lambda *a, **kw: ""

    await chat._run_batch([_entry()])

    assert session.calls == 1, f"{reason} must not be retried"
    assert len(chat.bot.sent) == 1


@pytest.mark.asyncio
async def test_reserve_latch_short_circuit_does_not_trigger_a_retry():
    """`_context_reserve_blocked` skips the session entirely — it is `reserve`."""
    session = ProbeSession([{"ok": True, "reason": None}])
    chat = _chat_with(session, runtime_id="claude")
    chat._activity_store.finish_activity = lambda *a, **kw: ""
    await chat.mark_context_reserve_blocked()

    await chat._run_batch([_entry()])

    assert session.calls == 0, "latched chat must not probe at all"
    assert len(chat.bot.sent) == 1
