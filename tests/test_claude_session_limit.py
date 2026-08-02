import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
)

from claude_session import (
    ClaudeSession,
    EXPECTED_CONTEXT_MODEL,
    EXPECTED_CONTEXT_TOKENS,
    EXPECTED_MAX_OUTPUT_TOKENS,
    NORMAL_TURN_RESERVE_TOKENS,
)


RAW_LIMIT = "You've hit your monthly spend limit · resets 2:20pm (Europe/Berlin)"
RAW_CONTEXT_LIMIT = "Prompt is too long"


def result(
    *,
    error=False,
    text=None,
    status=None,
    sid="sid-new",
    max_output=EXPECTED_MAX_OUTPUT_TOKENS,
):
    return ResultMessage(
        subtype="error_during_execution" if error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=error,
        num_turns=1,
        session_id=sid,
        result=text,
        api_error_status=status,
        model_usage=(
            None
            if error
            else {
                EXPECTED_CONTEXT_MODEL: {
                    "maxOutputTokens": max_output,
                }
            }
        ),
    )


class QueueClient:
    def __init__(self):
        self.events = asyncio.Queue()
        self.queries = []
        self.context_usage = None

    async def query(self, text):
        self.queries.append(text)

    async def receive_messages(self):
        while True:
            yield await self.events.get()

    async def get_context_usage(self):
        return self.context_usage


def make_session(tmp_path):
    # Production wiring always passes config.MODEL (bot.py -> ChatRegistry),
    # so tests must too — a fixture on a different model silently diverges
    # from the invariant under test.
    import config

    session = ClaudeSession(
        cwd=".", model=config.MODEL, session_file=tmp_path / "session"
    )
    client = QueueClient()
    session._client = client
    session._connected = True

    async def connected(**kwargs):
        return None

    session._ensure_connected = connected
    return session, client


async def collect(session, prompt="hello"):
    return [chunk async for chunk in session.send_message(prompt)]


@pytest.mark.asyncio
async def test_observed_cli_2_1_220_limit_is_one_normalized_terminal_chunk(tmp_path):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        AssistantMessage(
            content=[TextBlock(RAW_LIMIT)],
            model="<synthetic>",
            error="rate_limit",
        )
    )
    await client.events.put(result(error=True, text=RAW_LIMIT))

    chunks = await task

    assert chunks[0]["type"] == "error"
    assert chunks[0]["kind"] == "usage_limit"
    assert len(chunks) == 1
    assert session._expected_results == 0
    assert session._is_processing is False
    assert session.usage_limit_active is True


@pytest.mark.asyncio
async def test_typed_limit_without_late_result_error_is_still_terminal(tmp_path):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        AssistantMessage(
            content=[TextBlock(RAW_LIMIT)],
            model="<synthetic>",
            error="rate_limit",
        )
    )
    await client.events.put(result(error=False, text=None))

    chunks = await task

    assert [chunk["type"] for chunk in chunks] == ["error"]
    assert chunks[0]["kind"] == "usage_limit"
    assert session._expected_results == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["429", "blocking_limit"])
async def test_result_limit_variants_are_normalized(tmp_path, variant):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    terminal = result(error=True, status=429 if variant == "429" else None)
    if variant == "blocking_limit":
        terminal.terminal_reason = "blocking_limit"
    await client.events.put(terminal)

    chunks = await task

    assert chunks == [{"type": "error", "kind": "usage_limit", "content": "usage limit"}]
    assert session.usage_limit_active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_first", [True, False])
async def test_mixed_injected_batch_drains_all_results_and_keeps_latch(tmp_path, limit_first):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    assert await session.inject("injected") is True

    limit_messages = [
        AssistantMessage(
            content=[TextBlock(RAW_LIMIT)],
            model="<synthetic>",
            error="rate_limit",
        ),
        result(error=True, text=RAW_LIMIT, sid="sid-limit"),
    ]
    success_messages = [
        AssistantMessage(content=[TextBlock("ok")], model="claude"),
        result(sid="sid-success"),
    ]
    for message in (
        limit_messages + success_messages
        if limit_first
        else success_messages + limit_messages
    ):
        await client.events.put(message)

    chunks = await task

    errors = [chunk for chunk in chunks if chunk["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["kind"] == "usage_limit"
    assert session._expected_results == 0
    assert session.usage_limit_active is True


@pytest.mark.asyncio
async def test_terminal_result_and_inject_race_cannot_leave_stale_result(tmp_path):
    session, client = make_session(tmp_path)
    first = asyncio.create_task(collect(session, "first"))
    while not session._is_processing:
        await asyncio.sleep(0)

    await session._query_lock.acquire()
    try:
        await client.events.put(result(sid="sid-first"))
        injected = asyncio.create_task(session.inject("racing"))
        await asyncio.sleep(0)
    finally:
        session._query_lock.release()

    accepted = await injected
    if accepted:
        await client.events.put(result(sid="sid-injected"))
    await first

    assert session._expected_results == 0
    second = asyncio.create_task(collect(session, "second"))
    await asyncio.sleep(0)
    await client.events.put(result(sid="sid-second"))
    assert await second == []
    assert client.queries[-1] == "second"


@pytest.mark.asyncio
async def test_inflight_inject_is_rejected_if_stream_fails_during_query(tmp_path):
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    class FailingClient:
        async def query(self, text):
            if text == "racing":
                query_started.set()
                await release_query.wait()

        async def receive_messages(self):
            await query_started.wait()
            raise RuntimeError("stream failed while inject was in flight")
            yield

    session = ClaudeSession(cwd=".", session_file=tmp_path / "session")
    client = FailingClient()
    session._client = client
    session._connected = True

    async def connected(**_kwargs):
        return None

    session._ensure_connected = connected
    response = asyncio.create_task(collect(session, "first"))
    while not session._is_processing:
        await asyncio.sleep(0)
    injected = asyncio.create_task(session.inject("racing"))
    await query_started.wait()
    await asyncio.sleep(0)
    release_query.set()

    chunks, accepted = await asyncio.gather(response, injected)

    assert chunks == [
        {"type": "error", "content": "stream failed while inject was in flight"}
    ]
    assert accepted is False
    assert session._expected_results == 0
    assert session._is_processing is False


@pytest.mark.asyncio
async def test_allowed_other_scope_does_not_clear_rejection_but_successful_turn_does(tmp_path):
    session, client = make_session(tmp_path)
    limited = asyncio.create_task(collect(session, "limited"))
    await asyncio.sleep(0)
    await client.events.put(
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected", rate_limit_type="seven_day_opus"
            ),
            uuid="rejected",
            session_id="sid",
        )
    )
    await client.events.put(
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed", rate_limit_type="five_hour"
            ),
            uuid="allowed",
            session_id="sid",
        )
    )
    await client.events.put(result(error=True, text=RAW_LIMIT))
    await limited
    assert session.usage_limit_active is True

    successful = asyncio.create_task(collect(session, "success"))
    await asyncio.sleep(0)
    await client.events.put(result(sid="sid-success"))
    await successful
    assert session.usage_limit_active is False


def test_options_disable_native_auto_compact(tmp_path):
    session = ClaudeSession(cwd=".", session_file=tmp_path / "session")

    options = session._make_options()

    assert options.env["DISABLE_AUTO_COMPACT"] == "1"
    assert "DISABLE_COMPACT" not in options.env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "Prompt is too long",
        "Context exceeds the 1000000 token limit",
        "context window exceeds the maximum token limit",
    ],
)
async def test_context_limit_result_is_one_normalized_terminal_chunk(tmp_path, raw):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(result(error=True, text=raw))

    chunks = await task

    assert chunks == [{"type": "error", "kind": "context_limit", "content": raw}]
    assert session._expected_results == 0
    assert session._is_processing is False
    assert client.queries == ["hello"]


def context_usage(total, **overrides):
    usage = {
        "totalTokens": total,
        "maxTokens": EXPECTED_CONTEXT_TOKENS,
        "rawMaxTokens": EXPECTED_CONTEXT_TOKENS,
        "model": EXPECTED_CONTEXT_MODEL,
        "isAutoCompactEnabled": False,
    }
    usage.update(overrides)
    return usage


@pytest.mark.asyncio
async def test_reserve_exact_boundary_and_one_below(tmp_path):
    session, client = make_session(tmp_path)
    prompt = "Привет"
    required = NORMAL_TURN_RESERVE_TOKENS + len(prompt.encode("utf-8"))

    client.context_usage = context_usage(EXPECTED_CONTEXT_TOKENS - required)
    admitted = await session.check_context_reserve(prompt)
    client.context_usage = context_usage(
        EXPECTED_CONTEXT_TOKENS - required + 1
    )
    rejected = await session.check_context_reserve(prompt)

    assert admitted["ok"] is True
    assert admitted["remaining"] == required
    assert rejected["ok"] is False
    assert rejected["reason"] == "reserve"
    assert client.queries == []


@pytest.mark.asyncio
async def test_reserve_never_uses_previous_cache_when_fresh_usage_is_zero(tmp_path):
    session, client = make_session(tmp_path)
    session._last_ctx_usage = context_usage(100)
    client.context_usage = context_usage(0)

    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is False
    assert outcome["reason"] == "runtime_invariant"
    assert client.queries == []


@pytest.mark.asyncio
async def test_reserve_never_uses_previous_cache_when_fresh_usage_is_none(tmp_path):
    session, client = make_session(tmp_path)
    session._last_ctx_usage = context_usage(100)
    client.context_usage = None

    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is False
    assert outcome["reason"] == "unknown"
    assert client.queries == []


@pytest.mark.asyncio
async def test_manual_floor_boundary_and_fresh_session_admission(tmp_path):
    session, client = make_session(tmp_path)
    session.session_id = None
    client.context_usage = context_usage(920_000)

    admitted = await session.check_context_reserve(manual=True)
    client.context_usage = context_usage(920_001)
    rejected = await session.check_context_reserve(manual=True)

    assert admitted["ok"] is True
    assert admitted["remaining"] == 80_000
    assert rejected["reason"] == "reserve"
    assert client.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"model": "claude-opus-5"},
        {"maxTokens": 200_000},
        {"rawMaxTokens": 200_000},
        {"isAutoCompactEnabled": True},
        {"totalTokens": None},
    ],
)
async def test_reserve_runtime_invariant_mismatch_fails_closed(
    tmp_path, override
):
    session, client = make_session(tmp_path)
    client.context_usage = context_usage(10_000)
    client.context_usage.update(override)

    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is False
    assert outcome["reason"] == "runtime_invariant"
    assert client.queries == []


@pytest.mark.asyncio
async def test_terminal_max_output_mismatch_blocks_next_admission(tmp_path):
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(result(max_output=32_000))
    await task
    client.context_usage = context_usage(10_000)

    outcome = await session.check_context_reserve("hello")

    assert session._max_output_tokens_valid is False
    assert outcome["reason"] == "runtime_invariant"
    assert client.queries == ["hello"]


@pytest.mark.asyncio
async def test_stale_sid_preflight_preserves_sid_and_performs_zero_query(tmp_path):
    session, client = make_session(tmp_path)
    session.session_id = "sid-old"
    session._write_session_id("sid-old")

    async def stale(**_kwargs):
        raise RuntimeError("No conversation found for session sid-old")

    session._ensure_connected = stale
    outcome = await session.check_context_reserve("hello")

    assert outcome["reason"] == "session_unavailable"
    assert session.session_id == "sid-old"
    assert (tmp_path / "session").read_text() == "sid-old"
    assert client.queries == []


@pytest.mark.asyncio
async def test_stale_sid_control_failure_is_not_downgraded_to_unknown(tmp_path):
    session, client = make_session(tmp_path)
    session.session_id = "sid-old"
    session._write_session_id("sid-old")

    async def stale_usage():
        raise RuntimeError("No conversation found for session sid-old")

    client.get_context_usage = stale_usage
    outcome = await session.check_context_reserve("hello")

    assert outcome["reason"] == "session_unavailable"
    assert session.session_id == "sid-old"
    assert (tmp_path / "session").read_text() == "sid-old"
    assert client.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    ["No conversation found for session sid-old", "process exit code 1"],
)
async def test_send_message_never_invalidates_or_recursively_retries_sid(
    tmp_path, error
):
    session, client = make_session(tmp_path)
    session.session_id = "sid-old"
    session._write_session_id("sid-old")

    async def fail_query(text):
        client.queries.append(text)
        raise RuntimeError(error)

    client.query = fail_query
    chunks = await collect(session)

    assert chunks == [
        {"type": "error", "kind": "session_unavailable", "content": error}
    ]
    assert client.queries == ["hello"]
    assert session.session_id == "sid-old"
    assert (tmp_path / "session").read_text() == "sid-old"


def limit_result_without_model_usage(text=RAW_LIMIT):
    """The exact shape production returned on 2026-08-01.

    A plan-limit Result arrives with is_error=False and NO model_usage:
    the limit notice replaces the usage payload. Absent usage must never be
    read as proof that the runtime is wrong.
    """
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sid-new",
        result=text,
        api_error_status=None,
        model_usage={},
    )


@pytest.mark.asyncio
async def test_usage_limit_result_does_not_latch_runtime_invariant(tmp_path):
    """Production incident: a quota hit bricked the bot until restart.

    The limit Result carries no model_usage, the old code latched
    _max_output_tokens_valid=False, and every later message was rejected
    with runtime_invariant forever.
    """
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(limit_result_without_model_usage())
    await task

    assert session.usage_limit_active is True
    assert session._max_output_tokens_valid is True, (
        "a usage limit is a temporary condition and must not latch the "
        "runtime invariant"
    )

    client.context_usage = context_usage(10_000)
    outcome = await session.check_context_reserve("hello")

    # Codex [blocking]: asserting only "not runtime_invariant" accepted a
    # DIFFERENT permanent block. A quota hit must leave admission OPEN so the
    # next turn can actually reach Claude and clear usage_limit_active.
    assert outcome["ok"] is True, (
        f"a stale usage limit must not gate admission (got {outcome!r}); "
        "usage_limit_active is only cleared by a successful turn, so "
        "refusing here deadlocks the session until restart"
    )


@pytest.mark.asyncio
async def test_empty_model_usage_on_plain_result_does_not_latch(tmp_path):
    """Absent usage proves nothing about the runtime — only a contradiction does."""
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-new",
            result="",
            api_error_status=None,
            model_usage={},
        )
    )
    await task

    assert session._max_output_tokens_valid is True


@pytest.mark.asyncio
async def test_latch_clears_on_next_valid_usage(tmp_path):
    """A contradicted invariant fails closed but is not a one-way door.

    Recovery is operator-driven: /clear, a compact, or an in-flight turn can
    supply a good payload. A fresh user turn cannot, because admission blocks
    first — this test drives collect() directly for exactly that reason.
    """
    session, client = make_session(tmp_path)

    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(result(max_output=32_000))
    await task
    assert session._max_output_tokens_valid is False

    task = asyncio.create_task(collect(session, "second"))
    await asyncio.sleep(0)
    await client.events.put(result())
    await task

    assert session._max_output_tokens_valid is True, (
        "a proven-good usage payload must clear the latch without a restart"
    )
    client.context_usage = context_usage(10_000)
    outcome = await session.check_context_reserve("hello")
    assert outcome["ok"] is True


@pytest.mark.asyncio
async def test_reserve_admits_normal_message_on_real_config(tmp_path):
    """Config-driven: the model actually deployed must pass the reserve.

    The 167-test suite went green while production rejected every message,
    because every reserve test built usage from EXPECTED_CONTEXT_MODEL
    itself. This derives the runtime model from config.MODEL instead.
    """
    import config

    session, client = make_session(tmp_path)
    session.model = config.MODEL

    resolved = session._make_options().model
    assert resolved == EXPECTED_CONTEXT_MODEL, (
        f"config.MODEL={config.MODEL!r} resolves to {resolved!r} but the "
        f"reserve expects {EXPECTED_CONTEXT_MODEL!r} — these must be derived "
        f"from one source, not kept in sync by hand"
    )

    client.context_usage = context_usage(10_000, model=resolved)
    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is True, f"normal message rejected: {outcome!r}"
    assert outcome["reason"] is None


@pytest.mark.asyncio
async def test_terminal_usage_without_expected_model_latches(tmp_path):
    """Non-empty usage naming only other models IS drift evidence.

    Distinct from an empty payload: the runtime billed a model we did not ask
    for, so fail-closed is correct here.
    """
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-new",
            result="hi",
            api_error_status=None,
            model_usage={"claude-haiku-4-5-20251001": {"maxOutputTokens": 32_000}},
        )
    )
    await task

    assert session._max_output_tokens_valid is False


@pytest.mark.asyncio
async def test_quota_result_with_partial_usage_does_not_latch(tmp_path):
    """Codex round 2 [blocking]: a quota terminal may carry PARTIAL usage.

    Production's model_usage really does contain an auxiliary Haiku entry
    alongside the Opus one (measured live). A limit result carrying only that
    auxiliary entry is non-empty but omits the expected model — classifying it
    as drift would weld admission shut exactly like the original outage.
    """
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-new",
            result=RAW_LIMIT,
            api_error_status=None,
            model_usage={"claude-haiku-4-5-20251001": {"maxOutputTokens": 32_000}},
        )
    )
    await task

    assert session.usage_limit_active is True
    assert session._max_output_tokens_valid is True, (
        "a positively identified quota result must never mutate the runtime "
        "invariant, however partial its usage map"
    )

    client.context_usage = context_usage(10_000)
    outcome = await session.check_context_reserve("hello")
    assert outcome["ok"] is True, f"admission must stay open: {outcome!r}"


@pytest.mark.asyncio
async def test_quota_result_via_429_status_does_not_latch(tmp_path):
    """Same guarantee via the typed 429 path rather than the text detector."""
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-new",
            result="",
            api_error_status=429,
            model_usage={"claude-haiku-4-5-20251001": {"maxOutputTokens": 32_000}},
        )
    )
    await task

    assert session._max_output_tokens_valid is True


@pytest.mark.asyncio
async def test_context_limit_result_with_partial_usage_does_not_latch(tmp_path):
    """A context-limit short circuit must not latch either.

    Latching here would block the very /compact the message tells the user to
    run, turning a recoverable state into a dead end.
    """
    session, client = make_session(tmp_path)
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    await client.events.put(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-new",
            result=RAW_CONTEXT_LIMIT,
            api_error_status=None,
            model_usage={"claude-haiku-4-5-20251001": {"maxOutputTokens": 32_000}},
        )
    )
    await task

    assert session._max_output_tokens_valid is True


# --- #20: bounded context probe, honest reason, self-healing client ---


class HangingProbeClient(QueueClient):
    """A client whose control request never answers, like a wedged CLI.

    Measured in production: `get_context_usage` returns in 0.9-3.4s when healthy,
    but a stopped CLI burns the SDK's full 60s default before raising.
    """

    def __init__(self, *, hang_times=1, usage=None):
        super().__init__()
        self.hang_times = hang_times
        self.calls = 0
        self.usage = usage
        self.abandoned = 0

    async def get_context_usage(self):
        self.calls += 1
        if self.calls <= self.hang_times:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.abandoned += 1
                raise
        return self.usage


@pytest.fixture
def fast_probe(monkeypatch):
    """Shrink the probe budget so tests exercise logic, not wall-clock."""
    import claude_session
    monkeypatch.setattr(claude_session, "CONTEXT_PROBE_TIMEOUT_S", 0.05)


@pytest.mark.asyncio
async def test_probe_timeout_refuses_fast_instead_of_blocking_for_the_sdk_budget(tmp_path, fast_probe):
    """Two timeouts -> runtime_unhealthy, and the caller is not held for 60s."""
    session, _ = make_session(tmp_path)
    client = HangingProbeClient(hang_times=99)
    session._client = client

    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await session.check_context_reserve("hello")
    elapsed = loop.time() - started

    assert outcome["ok"] is False
    assert outcome["reason"] == "runtime_unhealthy"
    # generous ceiling: the point is "not the SDK's 60s", not a perf assertion
    assert elapsed < 30


@pytest.mark.asyncio
async def test_probe_succeeding_on_retry_admits_the_message(tmp_path, fast_probe):
    """One transient timeout must not cost the user their message."""
    session, _ = make_session(tmp_path)
    client = HangingProbeClient(hang_times=1, usage=context_usage(1000))
    session._client = client

    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_probe_timeout_does_not_leak_pending_control_entries(tmp_path, fast_probe):
    """The orphaned probe must be abandoned, never cancelled from outside.

    Cancelling from outside skips the SDK's own cleanup
    (`query.py:588-590`), leaking one pending entry per timeout on a session
    that lives for weeks. Measured 0->1->2->3 with a naive asyncio.wait_for.
    """
    session, _ = make_session(tmp_path)
    client = HangingProbeClient(hang_times=99)
    session._client = client

    await session.check_context_reserve("hello")
    await asyncio.sleep(0)

    assert client.abandoned == 0


@pytest.mark.asyncio
async def test_second_consecutive_timeout_reconnects_but_still_refuses(tmp_path, fast_probe):
    session, _ = make_session(tmp_path)
    client = HangingProbeClient(hang_times=99)
    session._client = client
    session.session_id = "sid-keep"

    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is False
    assert session._client is None          # reconnect() dropped the bad client
    assert session.session_id == "sid-keep"  # durable SID preserved


@pytest.mark.asyncio
async def test_single_timeout_does_not_reconnect(tmp_path, fast_probe):
    session, _ = make_session(tmp_path)
    client = HangingProbeClient(hang_times=1, usage=context_usage(1000))
    session._client = client

    await session.check_context_reserve("hello")

    assert session._client is client


@pytest.mark.asyncio
async def test_no_reconnect_while_session_replacement_is_active(tmp_path, fast_probe):
    """reconnect() swaps _client, which rollback compares by identity."""
    session, _ = make_session(tmp_path)
    client = HangingProbeClient(hang_times=99)
    session._client = client
    snapshot = session.begin_session_replacement()

    outcome = await session.check_context_reserve("hello")

    assert outcome["reason"] == "runtime_unhealthy"
    assert session._client is client
    assert session._session_replacement is snapshot


@pytest.mark.asyncio
async def test_full_context_still_refused_when_probe_works(tmp_path):
    """Hard constraint from #14: no new path admits into a full context."""
    session, client = make_session(tmp_path)
    client.context_usage = context_usage(EXPECTED_CONTEXT_TOKENS - 10)

    outcome = await session.check_context_reserve("hello")

    assert outcome["ok"] is False
    assert outcome["reason"] == "reserve"
