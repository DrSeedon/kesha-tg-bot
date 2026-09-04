"""Regressions retained after #34 removes the independent preventive timer."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_state import ChatPhase, ChatState, PendingEntry
from message_log import ActivityPersistenceError, MessageLog


NOW = datetime(2026, 9, 3, 16, 30, tzinfo=timezone.utc)


def _entry(mid=1):
    return PendingEntry(
        prompt="hi",
        message_id=mid,
        message=None,
        source="user",
        reply_target=1,
    )


def _state(tmp_path, *, store=None):
    store = store or MessageLog(tmp_path / "messages.db")
    session = MagicMock()
    session.session_id = None
    session.get_context_usage = AsyncMock(
        return_value={
            "percentage": 40.0,
            "totalTokens": 400_000,
            "maxTokens": 1_000_000,
        }
    )
    session.check_context_reserve = AsyncMock(
        return_value={"ok": True, "remaining": 900_000, "required": 80_000}
    )
    session.inject = AsyncMock(return_value=True)
    session.usage_limit_active = False
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=17))
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    compact = AsyncMock(
        return_value={"ok": True, "before_pct": 40.0, "after_pct": 4.0}
    )
    state = ChatState(
        chat_id=1,
        session=session,
        bot=bot,
        debounce_sec=60,
        ask_fn=AsyncMock(),
        set_current_chat_fn=MagicMock(),
        get_lazy_block_fn=MagicMock(return_value=("", [], [])),
        compact_session_fn=compact,
        activity_store=store,
        work_dir="/tmp",
    )
    return state, store, compact


@pytest.mark.asyncio
async def test_manual_compact_remains_time_independent(tmp_path):
    state, _store, compact = _state(tmp_path)

    assert await state.request_compact(automatic=False)

    compact.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_compact_below_floor_is_one_terminal_without_query(tmp_path):
    state, _store, compact = _state(tmp_path)
    state.session.check_context_reserve.return_value = {
        "ok": False,
        "reason": "reserve",
    }

    assert await state.request_compact(automatic=False)

    compact.assert_not_awaited()
    state.bot.send_message.assert_awaited_once()
    assert "/clear" in state.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_processing_arrival_is_deferred_without_injection(tmp_path):
    state, _store, _compact = _state(tmp_path)
    state.phase = ChatPhase.PROCESSING
    pending = _entry()

    await state.accept_entry(pending)

    assert state.deferred == [[pending]]
    state.session.inject.assert_not_awaited()


@pytest.mark.asyncio
async def test_processing_media_completion_is_deferred_without_injection(tmp_path):
    state, _store, _compact = _state(tmp_path)
    generation, media_generation = await state.media_started()
    state.phase = ChatPhase.PROCESSING
    pending = _entry()

    await state.media_finished(pending, generation, media_generation)

    assert state.deferred == [[pending]]
    state.session.inject.assert_not_awaited()


@pytest.mark.asyncio
async def test_processing_urgent_reminder_is_deferred_without_injection(tmp_path):
    state, _store, _compact = _state(tmp_path)
    state.phase = ChatPhase.PROCESSING

    await state.run_urgent_prompt("urgent")

    assert len(state.deferred) == 1
    assert state.deferred[0][0].prompt == "urgent"
    state.session.inject.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_finish_stays_nonquiescent(tmp_path):
    state, store, _compact = _state(tmp_path)
    store.begin_activity(1)
    state.phase = ChatPhase.PROCESSING
    store.finish_activity = MagicMock(
        side_effect=ActivityPersistenceError("write failed")
    )

    await state._drain_or_idle()

    assert state.phase is ChatPhase.IDLE


@pytest.mark.asyncio
async def test_next_successful_turn_clears_old_attempt_marker(tmp_path):
    state, store, _compact = _state(tmp_path)
    store.finish_activity(1, NOW)
    snapshot = store.get_activity(1)["last_activity_utc"]
    assert store.claim_auto_attempt(1, snapshot)

    store.begin_activity(1, NOW)
    state.phase = ChatPhase.PROCESSING
    await state._drain_or_idle()

    row = store.get_activity(1)
    assert row["quiescent"] == 1
    assert row["auto_attempted_for_utc"] is None


@pytest.mark.asyncio
async def test_media_is_durable_before_work_and_balanced_on_failure(tmp_path):
    state, store, _compact = _state(tmp_path)

    generation, media_generation = await state.media_started()

    assert store.get_activity(1)["quiescent"] == 0
    assert state.pending_transcriptions == 1
    await state.media_finished(None, generation, media_generation)
    assert store.get_activity(1)["quiescent"] == 1
    assert state.pending_transcriptions == 0


@pytest.mark.asyncio
async def test_compact_notifier_terminalizes_one_progress_message(tmp_path):
    state, _store, _compact = _state(tmp_path)
    notify = state._make_compact_notifier()

    await notify("start", replace=True)
    await notify("terminal", replace=True)

    state.bot.send_message.assert_awaited_once_with(1, "start")
    state.bot.edit_message_text.assert_awaited_once_with(
        "terminal", chat_id=1, message_id=17
    )
