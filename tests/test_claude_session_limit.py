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
    session = ClaudeSession(cwd=".", session_file=tmp_path / "session")
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
