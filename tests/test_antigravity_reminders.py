import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

import config
import reminders


@pytest.fixture
def stand(tmp_path, monkeypatch):
    db = reminders.ReminderDB(tmp_path / "reminders.sqlite")
    monkeypatch.setattr(config, "ALLOWED", {1})
    monkeypatch.setattr(reminders, "get_db", lambda: db)
    monkeypatch.setattr(reminders, "_runtime_for_chat", lambda _: "antigravity")
    bot = AsyncMock()
    return db, bot


def create(db, text, kind="urgent_llm", interval=None):
    return db.get(db.create(1, text, reminders.utc_now() - timedelta(seconds=1), kind, interval))


@pytest.mark.asyncio
async def test_owned_reminder_jitter_rate_repeat_and_plain_fallback(stand, monkeypatch):
    db, bot = stand
    original_sleep = asyncio.sleep
    jitter = []
    invoked = []

    async def sleep(seconds):
        jitter.append(seconds)
        await original_sleep(0)

    async def wake(chat_id, payload, *, reminder):
        assert reminder.consume(chat_id)
        assert not reminder.consume(chat_id)
        invoked.append(reminder.text)
        return True

    monkeypatch.setattr(reminders.random, "uniform", lambda low, high: (low + high) / 2)
    monkeypatch.setattr(reminders.asyncio, "sleep", sleep)
    monkeypatch.setattr(reminders, "_urgent_llm_handler", wake)

    rows = [create(db, f"urgent {index}") for index in range(9)]
    rows.append(create(db, "too frequent", interval="15m"))
    for row in rows:
        await reminders._fire_reminder(row, bot, None, db)
    for _ in range(30):
        if not db._agy_inflight:
            break
        await original_sleep(0)

    assert not db._agy_inflight
    assert len(invoked) == 8
    assert jitter == [13.5] * 9
    assert {call.args[1] for call in bot.send_message.await_args_list} == {"⏰ urgent 8", "⏰ too frequent"}
    assert db.get(rows[-1]["id"])["due_at"] > rows[-1]["due_at"]


@pytest.mark.asyncio
async def test_plain_lazy_and_missed_have_no_agent_wakeup(stand, monkeypatch):
    db, bot = stand
    wake = AsyncMock(side_effect=AssertionError("unexpected ACP wake"))
    monkeypatch.setattr(reminders, "_urgent_llm_handler", wake)
    plain, lazy = create(db, "plain test", "plain"), create(db, "lazy test", "lazy_llm")
    await reminders._fire_reminder(plain, bot, None, db)
    await reminders._fire_reminder(lazy, bot, None, db)
    lazy_block, ids, _ = reminders.get_lazy_block_for_prompt(1)
    assert ids == [lazy["id"]] and "lazy test" in lazy_block
    missed = create(db, "missed urgent")
    await reminders.deliver_missed_on_startup(bot, None, {1})
    assert "⏰ missed urgent" in bot.send_message.await_args.args[1]
    assert db.get(missed["id"])["delivered"] == 1
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_ticket_is_chat_bound_and_rechecks_row(stand):
    db, _ = stand
    row = create(db, "original")
    ticket = reminders.OwnedReminder(db, row)
    assert not ticket.admit(2)
    assert ticket.admit(1)
    db.update(row["id"], text="edited")
    assert not ticket.consume(1)


@pytest.mark.asyncio
async def test_failed_startup_digest_retries_plain_only(stand, monkeypatch):
    db, bot = stand
    wake = AsyncMock(side_effect=AssertionError("startup must not wake ACP"))
    monkeypatch.setattr(reminders, "_urgent_llm_handler", wake)
    missed = create(db, "missed")
    bot.send_message.side_effect = [RuntimeError("Telegram offline"), None]
    await reminders.deliver_missed_on_startup(bot, None, {1})
    assert db.get(missed["id"])["fired_at"] is None
    await reminders._fire_reminder(db.get(missed["id"]), bot, None, db)
    assert db.get(missed["id"])["delivered"] == 1
    assert bot.send_message.await_args.args[1] == "⏰ missed"
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_greet_on_antigravity_is_plain(monkeypatch):
    monkeypatch.setattr(config, "TOKEN", f"{1}:pytest")
    import bot as bot_module

    tg = AsyncMock()
    monkeypatch.setattr(bot_module, "STRINGS", {"ru": {"started": "started"}})
    urgent = AsyncMock(side_effect=AssertionError("greeting touched model"))
    await bot_module._greet_after_restart(tg, {1}, True, lambda _: "antigravity", urgent)
    tg.send_message.assert_awaited_once_with(1, "started")
    urgent.assert_not_awaited()



@pytest.mark.asyncio
async def test_stale_lazy_uses_the_same_guard_after_promotion(stand, monkeypatch):
    db, bot = stand
    stale = create(db, "old lazy", kind="lazy_llm")
    db.mark_fired(stale["id"])
    db.update(stale["id"], fired_at=reminders.utc_iso(reminders.utc_now() - timedelta(hours=25)))
    woke = AsyncMock()
    monkeypatch.setattr(reminders, "_urgent_llm_handler", woke)
    for index in range(8):
        assert db.reserve_agy_wake(create(db, f"existing {index}"))
    # Those unrelated occurrences already ran. Leave only the stale lazy for this tick.
    for row in db.fetch_pending_due(reminders.utc_now()):
        db.mark_fired(row["id"], delivered=True)
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds == reminders.TICK_SECONDS:
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(reminders.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await reminders.reminder_loop(bot, None, {1})
    for _ in range(8):
        if not db._agy_inflight:
            break
        await original_sleep(0)
    assert not db._agy_inflight
    row = db.get(stale["id"])
    assert row["promoted"] and row["delivered"]
    assert bot.send_message.await_args.args[1] == "⏰ old lazy"
    woke.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_backoff_resumes_from_first_unconfirmed_part(stand, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage
    import telegram_io

    db, bot = stand
    row = create(db, "long reminder", kind="plain")
    monkeypatch.setattr(telegram_io, "split_msg", lambda text: ["part one", "part two"])
    bot.send_message.side_effect = [
        None,
        TelegramRetryAfter(method=SendMessage(chat_id=1, text="part two"),
                           message="test flood", retry_after=60),
        None,
    ]
    await reminders._fire_reminder(row, bot, None, db)
    await reminders._fire_reminder(row, bot, None, db)
    assert bot.send_message.await_count == 2
    progress = db.delivery(row)
    assert progress["state"] == "retry" and progress["next_part"] == 1
    db.record_delivery(row, part=1, state="retry", attempts=1,
                       retry_at=reminders.utc_iso(reminders.utc_now() - timedelta(seconds=1)))
    await reminders._fire_reminder(row, bot, None, db)
    assert [call.args[1] for call in bot.send_message.await_args_list] == [
        "part one", "part two", "part two",
    ]
    assert db.get(row["id"])["delivered"] == 1
