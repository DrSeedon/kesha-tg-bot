import sqlite3
from datetime import datetime, timezone

import pytest

from message_log import (
    ActivityPersistenceError,
    MessageLog,
    RuntimeStatePersistenceError,
)


def utc(hour=0, minute=0):
    return datetime(2026, 7, 30, hour, minute, tzinfo=timezone.utc)


def test_activity_lifecycle_and_claim_survive_restart(tmp_path):
    path = tmp_path / "messages.db"
    first = MessageLog(path)

    admitted = first.begin_activity(7, utc(1))
    row = first.get_activity(7)
    assert row["last_activity_utc"] == admitted
    assert row["quiescent"] == 0

    completed = first.finish_activity(7, utc(2))
    assert first.claim_auto_attempt(7, completed) is True
    assert first.claim_auto_attempt(7, completed) is False
    first.conn.close()

    reopened = MessageLog(path)
    row = reopened.get_activity(7)
    assert row["quiescent"] == 1
    assert row["auto_attempted_for_utc"] == completed
    assert reopened.list_quiescent_chat_ids() == [7]

    next_activity = reopened.begin_activity(7, utc(3))
    row = reopened.get_activity(7)
    assert row["last_activity_utc"] == next_activity
    assert row["quiescent"] == 0
    assert row["auto_attempted_for_utc"] is None


def test_claim_loses_to_new_activity_snapshot(tmp_path):
    db = MessageLog(tmp_path / "messages.db")
    old = db.finish_activity(7, utc(1))
    db.begin_activity(7, utc(2))

    assert db.claim_auto_attempt(7, old) is False


def test_activity_migrates_intermediate_schema(tmp_path):
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE chat_activity (
            chat_id INTEGER PRIMARY KEY,
            last_activity_utc TEXT NOT NULL,
            quiescent INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    db = MessageLog(path)

    columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(chat_activity)")
    }
    assert "auto_attempted_for_utc" in columns


def test_activity_write_failure_is_typed_and_leaves_old_row(monkeypatch, tmp_path):
    db = MessageLog(tmp_path / "messages.db")
    old = db.finish_activity(7, utc(1))

    class FailingConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db, "conn", FailingConnection())

    with pytest.raises(ActivityPersistenceError, match="admission"):
        db.begin_activity(7, utc(2))

    reopened = MessageLog(tmp_path / "messages.db")
    row = reopened.get_activity(7)
    assert row["last_activity_utc"] == old
    assert row["quiescent"] == 1


def test_runtime_and_clear_floor_survive_restart(tmp_path):
    path = tmp_path / "messages.db"
    first = MessageLog(path)
    first.log_user(7, "old question")
    first.log_assistant(7, "old answer")
    first.set_runtime(7, "codex")

    floor = first.mark_history_cleared(7, "codex")
    first.log_user(7, "new question")
    first.conn.close()

    reopened = MessageLog(path)
    assert reopened.get_runtime(7) == "codex"
    assert reopened.get_history_floor(7) == floor
    rows = reopened.get_history(7, after_id=floor)
    assert [row["content"] for row in rows] == ["new question"]


def test_set_runtime_preserves_existing_clear_floor(tmp_path):
    db = MessageLog(tmp_path / "messages.db")
    db.log_user(7, "old")
    floor = db.mark_history_cleared(7, "claude")

    db.set_runtime(7, "codex")

    assert db.get_runtime(7) == "codex"
    assert db.get_history_floor(7) == floor


def test_runtime_write_failure_is_typed(monkeypatch, tmp_path):
    db = MessageLog(tmp_path / "messages.db")

    class FailingConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db, "conn", FailingConnection())

    with pytest.raises(RuntimeStatePersistenceError, match="runtime selection"):
        db.set_runtime(7, "codex")
