"""Preventive-compact idle timer (docs/tasks/cache-compact).

Timer arms on every incoming message; after PREVENTIVE_IDLE_MINUTES of silence it compacts
(if ctx > threshold and not busy) so the inevitable cold-start is cheap. Tests use a tiny
threshold + mocked session so they run in milliseconds.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import chat_state
from chat_state import ChatState, ChatPhase, PendingEntry


def _mk_state(ctx_pct=40.0):
    """Minimal ChatState with mocked collaborators. session.get_context_usage → ctx_pct."""
    session = MagicMock()
    session.get_context_usage = AsyncMock(return_value={"percentage": ctx_pct})
    session.inject = AsyncMock(return_value=True)
    cs = ChatState(
        chat_id=1, session=session, bot=MagicMock(), debounce_sec=0,
        auto_compact_pct=95.0, ask_fn=AsyncMock(), set_current_chat_fn=MagicMock(),
        get_lazy_block_fn=MagicMock(return_value=""), compact_session_fn=AsyncMock(return_value={"ok": True}),
        maybe_auto_compact_fn=AsyncMock(return_value=None), work_dir="/tmp",
    )
    return cs


def _user_entry(mid=1):
    return PendingEntry(prompt="hi", message_id=mid, message=None, source="user", reply_target=1)


@pytest.fixture(autouse=True)
def _fast_timer(monkeypatch):
    # 0.05s instead of 50min so tests are instant
    monkeypatch.setattr(chat_state, "PREVENTIVE_IDLE_MINUTES", 0.05 / 60)


@pytest.mark.asyncio
async def test_timer_arms_on_message():
    cs = _mk_state()
    assert cs._preventive_task is None
    await cs.accept_entry(_user_entry())
    assert cs._preventive_task is not None and not cs._preventive_task.done()
    cs._shutdown = True
    cs._preventive_task.cancel()


@pytest.mark.asyncio
async def test_fires_compact_after_idle():
    cs = _mk_state(ctx_pct=40.0)
    cs.request_compact = AsyncMock(return_value=True)
    await cs.accept_entry(_user_entry())
    # entry landed in pending (debounce), drain it so phase is IDLE when timer fires
    cs.pending.clear()
    cs.phase = ChatPhase.IDLE
    await asyncio.sleep(0.12)
    cs.request_compact.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_when_ctx_below_threshold():
    cs = _mk_state(ctx_pct=5.0)  # below PREVENTIVE_COMPACT_MIN_CTX (10)
    cs.request_compact = AsyncMock(return_value=True)
    cs.phase = ChatPhase.IDLE
    cs._arm_preventive_timer()
    await asyncio.sleep(0.12)
    cs.request_compact.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_busy():
    cs = _mk_state(ctx_pct=40.0)
    cs.request_compact = AsyncMock(return_value=True)
    cs.phase = ChatPhase.PROCESSING  # busy
    cs._arm_preventive_timer()
    await asyncio.sleep(0.12)
    cs.request_compact.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_message_resets_timer():
    cs = _mk_state(ctx_pct=40.0)
    cs.request_compact = AsyncMock(return_value=True)
    cs.phase = ChatPhase.IDLE
    cs._arm_preventive_timer()
    first = cs._preventive_task
    assert first is not None
    await asyncio.sleep(0.02)  # not yet elapsed
    cs._arm_preventive_timer()  # new message → reset (cancels `first`)
    await asyncio.sleep(0)      # let the cancellation propagate
    assert first.cancelled() or first.done()
    assert cs._preventive_task is not first
    await asyncio.sleep(0.12)
    cs.request_compact.assert_awaited_once()  # fires once, from the second timer
    cs._shutdown = True
    if cs._preventive_task and not cs._preventive_task.done():
        cs._preventive_task.cancel()


@pytest.mark.asyncio
async def test_shutdown_cancels_timer():
    cs = _mk_state(ctx_pct=40.0)
    cs.phase = ChatPhase.IDLE
    cs._arm_preventive_timer()
    task = cs._preventive_task
    assert task is not None
    cs._shutdown = True
    task.cancel()
    await asyncio.sleep(0)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_arm_noop_when_shutdown():
    cs = _mk_state()
    cs._shutdown = True
    cs._arm_preventive_timer()
    assert cs._preventive_task is None
