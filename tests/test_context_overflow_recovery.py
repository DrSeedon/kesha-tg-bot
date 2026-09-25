"""V-38: a chat whose context passed the CLI's request guard must recover.

Prod 25.09.2026: 979153/1M tokens, the CLI answered "Prompt is too long" locally
($0, terminal_reason=blocking_limit); we reported a subscription limit and our own
summary request could not enter either, so the chat was dead until /clear.
"""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

import config
from chat_state import ChatPhase
from claude_session import ClaudeSession, expected_max_output_tokens
from compact import compact_session
from test_auto_compact_admission import entry, make_state
from test_claude_session_limit import QueueClient, collect, make_session, result


def overflow_result(sid="sid-1"):
    terminal = result(error=True, text="Prompt is too long", sid=sid)
    terminal.terminal_reason = "blocking_limit"
    return terminal


@pytest.mark.asyncio
async def test_window_overflow_is_context_limit_not_subscription_limit(tmp_path):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        AssistantMessage(
            content=[TextBlock("Prompt is too long")],
            model="<synthetic>",
            error="invalid_request",
        )
    )
    await client.events.put(overflow_result())

    chunks = await task

    assert chunks == [
        {"type": "error", "kind": "context_limit", "content": "Prompt is too long"}
    ]
    assert session.usage_limit_active is False


@pytest.mark.asyncio
async def test_overflow_result_alone_is_context_limit(tmp_path):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(overflow_result())

    chunks = await task

    assert [c.get("kind") for c in chunks] == ["context_limit"]
    assert session.usage_limit_active is False


def test_expected_max_output_follows_the_model():
    assert expected_max_output_tokens("claude-opus-5-5[1m]") == 128_000
    assert expected_max_output_tokens("claude-opus-5-5") == 128_000
    assert expected_max_output_tokens("claude-opus-5") == 64_000


@pytest.mark.asyncio
async def test_opus_55_output_ceiling_does_not_latch_the_invariant(tmp_path):
    session, client = make_session(tmp_path)
    session.model = "claude-opus-5-5"
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(AssistantMessage(content=[TextBlock("hi")], model="claude"))
    await client.events.put(result(text="hi", max_output=128_000))
    await task
    assert session._max_output_tokens_valid is True

    # A genuine mismatch must still be caught.
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(AssistantMessage(content=[TextBlock("hi")], model="claude"))
    await client.events.put(result(text="hi", max_output=64_000))
    await task
    assert session._max_output_tokens_valid is False


def boundary(pre=979_153, post=2_719):
    return SystemMessage(
        subtype="compact_boundary",
        data={"compact_metadata": {"trigger": "manual", "pre_tokens": pre, "post_tokens": post}},
    )


@pytest.mark.asyncio
async def test_native_compact_counts_only_a_real_compact_boundary(tmp_path):
    session, client = make_session(tmp_path)
    await client.events.put(boundary())
    await client.events.put(result(text=""))
    done = await session.native_compact()
    assert client.queries == ["/compact"]
    assert done == {"ok": True, "pre_tokens": 979_153, "post_tokens": 2_719}
    assert session._is_processing is False

    session, client = make_session(tmp_path)
    await client.events.put(result(error=True, text="Error during compaction"))
    done = await session.native_compact()
    assert done["ok"] is False

    session, client = make_session(tmp_path)
    await client.events.put(result(text=""))  # returned, but nothing was compacted
    assert (await session.native_compact())["ok"] is False


class OverflowedSession(ClaudeSession):
    """Context past the CLI guard: any request we make is rejected locally."""

    def __init__(self, tmp_path, *, native_ok=True):
        super().__init__(cwd=".", session_file=tmp_path / "sid")
        self.session_id = "sid-old"
        self.total = 979_153
        self.native_ok = native_ok
        self.sent = []
        self.native_calls = 0
        self._max_output_tokens_valid = True

    def _pct(self):
        return self.total / 10_000

    async def get_context_usage(self, **_kwargs):
        return {"percentage": self._pct(), "totalTokens": self.total, "maxTokens": 1_000_000}

    async def check_context_reserve(self, combined="", *, manual=False):
        return {
            "ok": True,
            "reason": None,
            "should_compact": self._pct() >= config.AUTO_COMPACT_TRIGGER_PCT,
            "projected_tokens": self.total,
            "max_tokens": 1_000_000,
        }

    async def send_message(self, text):
        self.sent.append(text)
        if self.total > 977_000:
            yield {"type": "error", "kind": "context_limit", "content": "Prompt is too long"}
            return
        yield {"type": "turn_done"}

    async def native_compact(self):
        self.native_calls += 1
        if not self.native_ok:
            return {"ok": False, "error": "no compact_boundary"}
        self.total = 2_719
        return {"ok": True, "pre_tokens": 979_153, "post_tokens": 2_719}


class Notices:
    def __init__(self):
        self.items = []

    async def __call__(self, text, *, replace):
        self.items.append(text)


@pytest.mark.asyncio
async def test_overflowed_session_is_compacted_natively_and_original_batch_is_sent(
    tmp_path, monkeypatch
):
    session = OverflowedSession(tmp_path)
    asked = []

    async def ask(_message, text, _chat_id):
        asked.append(text)

    state = make_state(session, compact_session, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_a, **_k: None),
    )

    await state._run_batch([entry("ORIGINAL", 1)])

    assert session.native_calls == 1
    assert len(asked) == 1 and "ORIGINAL" in asked[0]
    assert session.total == 2_719
    visible = " ".join(state.bot.sent + state.bot.edited)
    assert "лимит" not in visible.casefold()


@pytest.mark.asyncio
async def test_failed_native_compact_ends_in_clear_prompt_not_a_limit_message(
    tmp_path, monkeypatch
):
    session = OverflowedSession(tmp_path, native_ok=False)
    asked = []

    async def ask(_message, text, _chat_id):
        asked.append(text)

    state = make_state(session, compact_session, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_a, **_k: None),
    )

    await state._run_batch([entry("ORIGINAL", 1)])

    assert session.native_calls == 1
    assert asked == []
    visible = " ".join(state.bot.sent + state.bot.edited)
    assert "/clear" in visible
    assert "лимит" not in visible.casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["usage_limit", None])
async def test_native_hatch_opens_only_for_context_limit(tmp_path, kind):
    session = OverflowedSession(tmp_path)

    async def send(text):
        session.sent.append(text)
        yield {"type": "error", "kind": kind, "content": "boom"}

    session.send_message = send
    outcome = await compact_session(session, notify=Notices())

    assert outcome["ok"] is False
    assert session.native_calls == 0


@pytest.mark.asyncio
async def test_post_turn_check_compacts_when_one_turn_jumped_past_the_trigger(
    tmp_path, monkeypatch
):
    session = OverflowedSession(tmp_path)
    session.total = 500_000
    compacted = []

    async def compact(sess, notify=None, recent_rows=None):
        compacted.append(sess.total)
        sess.total = 40_000
        return {"ok": True}

    async def ask(_message, _text, _chat_id):
        session.total = 930_000  # the turn itself grew the context

    state = make_state(session, compact, ask)
    state.phase = ChatPhase.PROCESSING
    monkeypatch.setattr(
        "message_log.get_db",
        lambda: SimpleNamespace(log_user=lambda *_a, **_k: None),
    )

    await state._run_batch([entry("HEAVY", 1)])

    assert compacted == [930_000]
    assert state.phase is ChatPhase.IDLE
