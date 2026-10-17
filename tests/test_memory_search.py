"""Chat-history text search (memory_search.py) and its tool wrappers."""
import sqlite3

import pytest

import kesha_tools as kt
import memory_search as ms


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "messages.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, message_id INTEGER, timestamp TEXT NOT NULL)")
    rows = [
        (100, "user", "купил фиолетовую куртку в сентябре", "2026-09-01T10:00:00"),
        (100, "assistant", "сиреневый тоже хорошо смотрится", "2026-09-02T10:00:00"),
        (100, "user", "завтра дача", "2026-09-03T10:00:00"),
        (100, "user", "ещё про сиреневый цвет " + "x" * 400, "2026-10-01T10:00:00"),
        (200, "user", "чужой чат: фиолетовый секрет", "2026-09-01T10:00:00"),
    ]
    con.executemany("INSERT INTO messages(chat_id, role, content, timestamp) VALUES(?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def test_other_chat_is_never_returned(db):
    out = ms.search_history(db, 100, "секрет")
    assert out == "0 matches"
    assert "чужой" not in ms.search_history(db, 100, "фиолетов")


def test_alternation_matches_stems_newest_first(db):
    out = ms.search_history(db, 100, "фиолетов|сиренев").splitlines()
    assert out[0].startswith("3 matches")
    assert out[1].startswith("[4 |") and out[3].startswith("[1 |")


def test_filters_role_and_dates(db):
    assert ms.search_history(db, 100, "сиренев", role="assistant").startswith("1 matches")
    assert ms.search_history(db, 100, "сиренев", since="2026-09-15").startswith("1 matches")
    assert ms.search_history(db, 100, "сиренев", until="2026-09-02").startswith("1 matches")


def test_invalid_regex_falls_back_to_literal(db):
    assert ms.search_history(db, 100, "(дача") == "0 matches"
    assert ms.search_history(db, 100, "дача").startswith("1 matches")


def test_limit_and_snippet_are_bounded(db):
    out = ms.search_history(db, 100, "сиренев", limit=1).splitlines()
    assert out[0] == "2 matches, showing 1 newest"
    assert len(out) == 2 and len(out[1]) < 2 * ms.SNIPPET + 80


def test_read_messages_neighbours_stay_in_chat(db):
    out = ms.read_messages(db, 100, 3, before=1, after=1)
    assert out.count("[") == 3 and "[2 |" in out and "[4 |" in out
    assert ms.read_messages(db, 100, 5) == "no such message"


@pytest.mark.asyncio
async def test_tool_uses_current_chat_not_args(db, monkeypatch):
    import message_log
    monkeypatch.setattr(message_log, "DB_PATH", db)
    kt.set_current_chat(200)
    res = await kt.search_memory.handler({"pattern": "фиолетов", "chat_id": 100})
    assert "секрет" in res["content"][0]["text"] and "куртку" not in res["content"][0]["text"]
