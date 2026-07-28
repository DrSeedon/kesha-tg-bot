import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
)

from claude_session import ClaudeSession


RAW_LIMIT = "You've hit your monthly spend limit · resets 2:20pm (Europe/Berlin)"


def result(*, error=False, text=None, status=None, sid="sid-new"):
    return ResultMessage(
        subtype="error_during_execution" if error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=error,
        num_turns=1,
        session_id=sid,
        result=text,
        api_error_status=status,
    )


class QueueClient:
    def __init__(self):
        self.events = asyncio.Queue()
        self.queries = []

    async def query(self, text):
        self.queries.append(text)

    async def receive_messages(self):
        while True:
            yield await self.events.get()


def make_session(tmp_path):
    session = ClaudeSession(cwd=".", session_file=tmp_path / "session")
    client = QueueClient()
    session._client = client
    session._connected = True

    async def connected():
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

    async def connected():
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
