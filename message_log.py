"""Message log — SQLite storage for all chat messages (user prompts + assistant responses)."""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("kesha.message_log")

DB_PATH = Path("./storage/messages.db")

# callable(message_id, chat_id, role, content) — set by bot.py for RAG indexing
OnMessage = Callable[[int, int, str, str], None]


class ActivityPersistenceError(RuntimeError):
    """Durable conversation activity could not be recorded."""


class MessageLog:
    def __init__(self, path: Path = DB_PATH):
        self._on_message: Optional[OnMessage] = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                message_id INTEGER,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, timestamp);
            CREATE TABLE IF NOT EXISTS chat_activity (
                chat_id INTEGER PRIMARY KEY,
                last_activity_utc TEXT NOT NULL,
                quiescent INTEGER NOT NULL CHECK (quiescent IN (0, 1)),
                auto_attempted_for_utc TEXT
            );
        """)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(chat_activity)")
        }
        if "auto_attempted_for_utc" not in columns:
            self.conn.execute(
                "ALTER TABLE chat_activity ADD COLUMN auto_attempted_for_utc TEXT"
            )

    @staticmethod
    def _activity_time(now_utc: Optional[datetime]) -> str:
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("activity timestamp must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def begin_activity(self, chat_id: int, now_utc: Optional[datetime] = None) -> str:
        timestamp = self._activity_time(now_utc)
        try:
            self.conn.execute(
                """
                INSERT INTO chat_activity(
                    chat_id, last_activity_utc, quiescent, auto_attempted_for_utc
                ) VALUES(?, ?, 0, NULL)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_activity_utc=excluded.last_activity_utc,
                    quiescent=0,
                    auto_attempted_for_utc=NULL
                """,
                (chat_id, timestamp),
            )
        except sqlite3.Error as exc:
            raise ActivityPersistenceError("activity admission write failed") from exc
        return timestamp

    def finish_activity(self, chat_id: int, now_utc: Optional[datetime] = None) -> str:
        timestamp = self._activity_time(now_utc)
        try:
            self.conn.execute(
                """
                INSERT INTO chat_activity(
                    chat_id, last_activity_utc, quiescent, auto_attempted_for_utc
                ) VALUES(?, ?, 1, NULL)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_activity_utc=excluded.last_activity_utc,
                    quiescent=1,
                    auto_attempted_for_utc=NULL
                """,
                (chat_id, timestamp),
            )
        except sqlite3.Error as exc:
            raise ActivityPersistenceError("activity completion write failed") from exc
        return timestamp

    def claim_auto_attempt(self, chat_id: int, last_activity_utc: str) -> bool:
        try:
            cursor = self.conn.execute(
                """
                UPDATE chat_activity
                SET auto_attempted_for_utc=last_activity_utc
                WHERE chat_id=?
                  AND last_activity_utc=?
                  AND quiescent=1
                  AND auto_attempted_for_utc IS NULL
                """,
                (chat_id, last_activity_utc),
            )
        except sqlite3.Error as exc:
            raise ActivityPersistenceError("automatic compact claim failed") from exc
        return cursor.rowcount == 1

    def get_activity(self, chat_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM chat_activity WHERE chat_id=?",
            (chat_id,),
        ).fetchone()

    def list_quiescent_chat_ids(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT chat_id FROM chat_activity WHERE quiescent=1"
        ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def set_on_message(self, cb: Optional[OnMessage]) -> None:
        """Late-bind RAG indexing callback (set from bot.py after event loop is up)."""
        self._on_message = cb

    def _notify(self, rid: int, chat_id: int, role: str, content: str) -> None:
        if not self._on_message:
            return
        try:
            self._on_message(rid, chat_id, role, content)
        except Exception as e:
            logger.error(f"on_message callback failed (id={rid}): {e}")

    def log_user(self, chat_id: int, content: str, msg_id: int = 0) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(chat_id, role, content, message_id) VALUES(?, 'user', ?, ?)",
            (chat_id, content, msg_id or None),
        )
        rid = int(cur.lastrowid or 0)
        self._notify(rid, chat_id, "user", content)
        return rid

    def log_assistant(self, chat_id: int, content: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(chat_id, role, content) VALUES(?, 'assistant', ?)",
            (chat_id, content),
        )
        rid = int(cur.lastrowid or 0)
        self._notify(rid, chat_id, "assistant", content)
        return rid

    def log_system(self, chat_id: int, content: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(chat_id, role, content) VALUES(?, 'system', ?)",
            (chat_id, content),
        )
        return int(cur.lastrowid or 0)

    def get_history(self, chat_id: int, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (chat_id, limit, offset),
        ).fetchall()

    def search(self, chat_id: int, query: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND content LIKE ? ORDER BY id DESC LIMIT ?",
            (chat_id, f"%{query}%", limit),
        ).fetchall()


_db: Optional[MessageLog] = None


def get_db() -> MessageLog:
    global _db
    if _db is None:
        _db = MessageLog()
    return _db
