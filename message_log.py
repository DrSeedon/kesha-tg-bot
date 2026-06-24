"""Message log — SQLite storage for all chat messages (user prompts + assistant responses)."""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger("kesha.message_log")

DB_PATH = Path("./storage/messages.db")


class MessageLog:
    def __init__(self, path: Path = DB_PATH):
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
        """)

    def log_user(self, chat_id: int, content: str, msg_id: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO messages(chat_id, role, content, message_id) VALUES(?, 'user', ?, ?)",
            (chat_id, content, msg_id or None),
        )

    def log_assistant(self, chat_id: int, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages(chat_id, role, content) VALUES(?, 'assistant', ?)",
            (chat_id, content),
        )

    def log_system(self, chat_id: int, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages(chat_id, role, content) VALUES(?, 'system', ?)",
            (chat_id, content),
        )

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
