import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import chat_state
from chat_state import (
    ChatPhase,
    ChatState,
    PendingEntry,
    _is_auto_compact_night,
)
from message_log import ActivityPersistenceError, MessageLog


NIGHT = datetime(2026, 7, 30, 16, 30, tzinfo=timezone.utc)  # 23:30 Krasnoyarsk
DAY = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)  # 12:00 Krasnoyarsk


def _entry(mid=1):
    return PendingEntry(
        prompt="hi",
        message_id=mid,
        message=None,
        source="user",
        reply_target=1,
    )


def _state(tmp_path, *, pct=40.0, now=NIGHT, store=None, seed=True):
    store = store or MessageLog(tmp_path / "messages.db")
    session = MagicMock()
    session.session_id = "sid-old"
    session.get_context_usage = AsyncMock(return_value={"percentage": pct})
    session.inject = AsyncMock(return_value=True)
    session.usage_limit_active = False
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=17))
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    compact = AsyncMock(return_value={"ok": True, "before_pct": pct, "after_pct": 4})
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
    if seed:
        store.finish_activity(1, now - timedelta(minutes=56))
    return state, store, compact


def _set_now(monkeypatch, value):
    monkeypatch.setattr(chat_state, "_utc_now", lambda: value)


@pytest.mark.parametrize("hour, expected", [(22, False), (23, True), (7, True), (8, False)])
def test_night_window_boundaries(hour, expected):
    local = datetime(2026, 7, 30, hour, tzinfo=chat_state.AUTO_COMPACT_TZ)
    assert _is_auto_compact_night(local.astimezone(timezone.utc)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("pct", [20.0, 95.0, 100.0])
async def test_daytime_never_compacts_at_any_context(tmp_path, monkeypatch, pct):
    state, store, compact = _state(tmp_path, pct=pct, now=DAY)
    _set_now(monkeypatch, DAY)
    snapshot = store.get_activity(1)["last_activity_utc"]

    await state._reserve_automatic_probe(snapshot)

    compact.assert_not_awaited()
    state.session.get_context_usage.assert_not_awaited()
    assert store.get_activity(1)["auto_attempted_for_utc"] is None


@pytest.mark.asyncio
async def test_night_idle_known_context_runs_once_and_marks_episode(tmp_path, monkeypatch):
    state, store, compact = _state(tmp_path, pct=20.0)
    _set_now(monkeypatch, NIGHT)
    snapshot = store.get_activity(1)["last_activity_utc"]

    await state._reserve_automatic_probe(snapshot)

    compact.assert_awaited_once()
    assert compact.await_args.kwargs["allow_native_recovery"] is False
    assert store.get_activity(1)["auto_attempted_for_utc"] == snapshot
    assert state.phase is ChatPhase.IDLE


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {"percentage": 19.9}])
async def test_unknown_or_low_context_claims_episode_without_compact(
    tmp_path, monkeypatch, usage
):
    state, store, compact = _state(tmp_path)
    state.session.get_context_usage.return_value = usage
    _set_now(monkeypatch, NIGHT)
    snapshot = store.get_activity(1)["last_activity_utc"]

    await state._reserve_automatic_probe(snapshot)

    compact.assert_not_awaited()
    assert store.get_activity(1)["auto_attempted_for_utc"] == snapshot
    state._arm_auto_compact()
    assert state._auto_compact_task is None


@pytest.mark.asyncio
async def test_daytime_scheduler_sleeps_until_23_local(tmp_path, monkeypatch):
    state, _store, compact = _state(tmp_path, now=DAY)
    _set_now(monkeypatch, DAY)
    delays = []

    async def capture_sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_state.asyncio, "sleep", capture_sleep)
    await state._run_auto_compact_scheduler()

    assert delays == [11 * 60 * 60]
    compact.assert_not_awaited()


@pytest.mark.asyncio
async def test_claimed_episode_stays_disarmed_after_cancellation_and_restart(
    tmp_path, monkeypatch
):
    state, store, compact = _state(tmp_path)
    _set_now(monkeypatch, NIGHT)
    snapshot = store.get_activity(1)["last_activity_utc"]
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def blocked_probe(**_kwargs):
        entered.set()
        await blocker.wait()

    state.session.get_context_usage.side_effect = blocked_probe
    task = asyncio.create_task(state._reserve_automatic_probe(snapshot))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    compact.assert_not_awaited()
    assert state.phase is ChatPhase.IDLE
    reopened = MessageLog(tmp_path / "messages.db")
    assert reopened.get_activity(1)["auto_attempted_for_utc"] == snapshot
    restarted, _store, _compact = _state(
        tmp_path / "restart",
        store=reopened,
        seed=False,
    )
    restarted._arm_auto_compact()
    assert restarted._auto_compact_task is None


@pytest.mark.asyncio
async def test_usage_limit_latch_skips_before_claim_and_probe(tmp_path, monkeypatch):
    state, store, compact = _state(tmp_path)
    state.session.usage_limit_active = True
    _set_now(monkeypatch, NIGHT)
    snapshot = store.get_activity(1)["last_activity_utc"]

    await state._reserve_automatic_probe(snapshot)

    compact.assert_not_awaited()
    state.session.get_context_usage.assert_not_awaited()
    assert store.get_activity(1)["auto_attempted_for_utc"] is None


@pytest.mark.asyncio
async def test_activity_during_probe_cancels_automatic_attempt(tmp_path, monkeypatch):
    state, store, compact = _state(tmp_path)
    _set_now(monkeypatch, NIGHT)
    snapshot = store.get_activity(1)["last_activity_utc"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def probe(**_kwargs):
        entered.set()
        await release.wait()
        return {"percentage": 95.0}

    state.session.get_context_usage.side_effect = probe
    state._drain_or_idle = AsyncMock()
    task = asyncio.create_task(state._reserve_automatic_probe(snapshot))
    await entered.wait()
    await state.accept_entry(_entry())
    release.set()
    await task

    compact.assert_not_awaited()
    assert state.deferred == [[_entry()]]
    state._drain_or_idle.assert_awaited_once_with(record_activity=False)


@pytest.mark.asyncio
async def test_manual_during_low_probe_wins_and_uses_native_recovery(
    tmp_path, monkeypatch
):
    state, store, compact = _state(tmp_path, pct=5.0)
    _set_now(monkeypatch, NIGHT)
    snapshot = store.get_activity(1)["last_activity_utc"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def probe(**_kwargs):
        entered.set()
        await release.wait()
        return {"percentage": 5.0}

    state.session.get_context_usage.side_effect = probe
    task = asyncio.create_task(state._reserve_automatic_probe(snapshot))
    await entered.wait()
    assert await state.request_compact(automatic=False)
    release.set()
    await task

    compact.assert_awaited_once()
    assert compact.await_args.kwargs["allow_native_recovery"] is True
    assert state.phase is ChatPhase.IDLE
    if state._auto_compact_task:
        state._auto_compact_task.cancel()


@pytest.mark.asyncio
async def test_manual_compact_works_during_day(tmp_path, monkeypatch):
    state, _store, compact = _state(tmp_path, now=DAY)
    _set_now(monkeypatch, DAY)

    assert await state.request_compact(automatic=False)

    compact.assert_awaited_once()
    assert compact.await_args.kwargs["allow_native_recovery"] is True
    if state._auto_compact_task:
        state._auto_compact_task.cancel()


@pytest.mark.asyncio
async def test_failed_finish_stays_nonquiescent_and_does_not_arm(tmp_path):
    state, store, _compact = _state(tmp_path)
    store.begin_activity(1)
    state.phase = ChatPhase.PROCESSING
    state._arm_auto_compact = MagicMock()
    store.finish_activity = MagicMock(
        side_effect=ActivityPersistenceError("write failed")
    )

    await state._drain_or_idle()

    assert state.phase is ChatPhase.IDLE
    state._arm_auto_compact.assert_not_called()


@pytest.mark.asyncio
async def test_next_successful_turn_clears_attempt_marker(tmp_path):
    state, store, _compact = _state(tmp_path)
    snapshot = store.get_activity(1)["last_activity_utc"]
    assert store.claim_auto_attempt(1, snapshot)

    store.begin_activity(1, NIGHT)
    state.phase = ChatPhase.PROCESSING
    state.session.session_id = None
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
