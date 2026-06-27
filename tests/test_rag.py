"""Tests for rag.py — index/search/RRF/backfill/idempotency/isolation.

Embeds real text via FastEmbed (model downloaded once, cached). Data-layer TDD.
"""

import sqlite3
from pathlib import Path

import pytest

import rag


def _make_messages_db(path: Path, rows: list[tuple]) -> None:
    """rows: (chat_id, role, content)"""
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            message_id INTEGER,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );
    """)
    con.executemany("INSERT INTO messages(chat_id, role, content) VALUES(?,?,?)", rows)
    con.commit()
    con.close()


@pytest.fixture
def mem(tmp_path):
    msg_db = tmp_path / "messages.db"
    _make_messages_db(msg_db, [
        (100, "user", "я люблю программировать на питоне и пишу backend"),
        (100, "assistant", "питон отличный выбор для бэкенда, держи совет"),
        (100, "user", "завтра еду на дачу копать картошку"),
        (200, "user", "совсем другой чат другого юзера про машины"),
        (100, "system", "служебное сообщение не должно индексироваться"),
        (100, "user", "   "),  # whitespace-only
    ])
    m = rag.RagMemory(path=tmp_path / "vec.db", msg_db=msg_db)
    return m


def test_index_and_search_semantic(mem):
    # index real messages
    rows = mem.conn.execute("SELECT id, chat_id, role, content FROM msg.messages").fetchall()
    for r in rows:
        mem.index_message(r["id"], r["chat_id"], r["role"], r["content"])
    # semantic query: should find python message even without exact words
    res = mem.search(100, "какой язык программирования я использую", limit=3)
    assert res, "expected matches"
    top_contents = " ".join(r["content"] for r in res)
    assert "питон" in top_contents.lower()


def test_skips_system_and_empty(mem):
    mem.index_message(5, 100, "system", "служебное")
    mem.index_message(6, 100, "user", "   ")
    cnt = mem.conn.execute("SELECT count(*) c FROM indexed").fetchone()["c"]
    assert cnt == 0


def test_idempotent_index(mem):
    mem.index_message(1, 100, "user", "тестовое сообщение")
    mem.index_message(1, 100, "user", "тестовое сообщение")  # dup
    cnt = mem.conn.execute("SELECT count(*) c FROM vec_messages").fetchone()["c"]
    assert cnt == 1


def test_chat_isolation(mem):
    for r in mem.conn.execute("SELECT id, chat_id, role, content FROM msg.messages").fetchall():
        mem.index_message(r["id"], r["chat_id"], r["role"], r["content"])
    # chat 200 search must never return chat 100 messages
    res = mem.search(200, "программирование питон", limit=5)
    for r in res:
        assert r["chat_id"] == 200


def test_role_filter(mem):
    for r in mem.conn.execute("SELECT id, chat_id, role, content FROM msg.messages").fetchall():
        mem.index_message(r["id"], r["chat_id"], r["role"], r["content"])
    res = mem.search(100, "питон бэкенд", limit=5, role="assistant")
    assert res
    assert all(r["role"] == "assistant" for r in res)


def test_backfill_idempotent(mem):
    n1 = mem.backfill()
    n2 = mem.backfill()
    # 3 valid user/assistant msgs in chat 100 + 1 in chat 200 = 4 (system+whitespace skipped)
    assert n1 == 4
    assert n2 == 0


def test_empty_query_returns_empty(mem):
    mem.index_message(1, 100, "user", "что-то")
    assert mem.search(100, "   ") == []


def test_schema_migration_drops_old(tmp_path):
    """Старый vec.db (user_version != SCHEMA_VERSION) → дроп+ребилд, stale data убрана."""
    import sqlite3
    import sqlite_vec

    msg = tmp_path / "messages.db"
    _make_messages_db(msg, [(100, "user", "x")])
    vec = tmp_path / "vec.db"
    # старая схема: fts без role, user_version=0, мусор в indexed
    old = sqlite3.connect(str(vec))
    old.enable_load_extension(True)
    sqlite_vec.load(old)
    old.enable_load_extension(False)
    old.execute("CREATE VIRTUAL TABLE fts_messages USING fts5(content, chat_id UNINDEXED, message_id UNINDEXED)")
    old.execute("CREATE TABLE indexed(message_id INTEGER PRIMARY KEY)")
    old.execute("INSERT INTO indexed VALUES(999)")
    old.commit()
    old.close()

    m = rag.RagMemory(path=vec, msg_db=msg)
    assert m.conn.execute("PRAGMA user_version").fetchone()[0] == rag.SCHEMA_VERSION
    assert m.conn.execute("SELECT count(*) FROM indexed").fetchone()[0] == 0
    cols = [r[1] for r in m.conn.execute("PRAGMA table_info(fts_messages)").fetchall()]
    assert "role" in cols


def test_rrf_merge():
    # vec ranks: a,b,c ; fts ranks: c,a ; c appears high in both → should rank first or near
    merged = rag.RagMemory._rrf([1, 2, 3], [3, 1])
    assert set(merged) == {1, 2, 3}
    # id 1 (rank0 vec + rank1 fts) and id 3 (rank2 vec + rank0 fts) outrank id 2 (rank1 vec only)
    assert merged.index(2) == len(merged) - 1


def test_chunk_short_vs_long():
    assert rag._chunk("привет как дела") == ["привет как дела"]
    long = "слово " * 400  # ~2400 символов > CHUNK_CHAR_LIMIT
    chunks = rag._chunk(long)
    assert len(chunks) > 1
    assert all(len(c) <= rag.CHUNK_SIZE + rag.CHUNK_OVERLAP + 50 for c in chunks)


def test_dedup_preserves_best_rank():
    assert rag._dedup([5, 3, 5, 7, 3, 1]) == [5, 3, 7, 1]


def test_chunk_caps_at_stride():
    """Экстремально длинный текст не должен дать idx >= CHUNK_STRIDE (chunk_id collision)."""
    huge = "слово " * 500_000  # ~3M символов
    chunks = rag._chunk(huge)
    assert len(chunks) <= rag.CHUNK_STRIDE - 1


def test_chunk_splits_oversized_token():
    """Один токен длиннее CHUNK_SIZE (URL/blob) режется, не обходит лимит."""
    blob = "x" * (rag.CHUNK_SIZE * 3)
    chunks = rag._chunk(blob)
    assert len(chunks) > 1
    assert all(len(c) <= rag.CHUNK_SIZE + rag.CHUNK_OVERLAP + 50 for c in chunks)


def test_expand_query_prefix():
    assert rag.RagMemory._expand_query("ссора Катей") == '"ссора"* OR "Катей"*'
    assert rag.RagMemory._expand_query("я и") is None  # все слова <3 символов
    assert rag.RagMemory._expand_query("") is None


def test_long_message_chunks_one_indexed(tmp_path):
    """Длинное сообщение → несколько vec-строк, но одна запись в indexed (idempotency по parent)."""
    class StubRag(rag.RagMemory):
        def _embed(self, texts, is_query):
            return [[0.0] * rag.DIM for _ in texts]

    msg = tmp_path / "messages.db"
    _make_messages_db(msg, [(100, "user", "слово " * 400)])
    m = StubRag(path=tmp_path / "vec.db", msg_db=msg)
    n = m.backfill()
    assert n == 1
    assert m.conn.execute("SELECT count(*) FROM vec_messages").fetchone()[0] > 1
    assert m.conn.execute("SELECT count(*) FROM indexed").fetchone()[0] == 1
    # повторный backfill идемпотентен
    assert m.backfill() == 0
