"""Tests for file RAG (task #10) — chunkers, index/dedup/delete, unified search, backfill.

Embeds real text via FastEmbed (model cached). Data-layer TDD, mirrors test_rag.py style.
"""

import sqlite3
from pathlib import Path

import pytest

import rag


# ---------------------------------------------------------------- T1: chunkers

MD_SAMPLE = """# Physics

Intro paragraph about physics that is reasonably long so the section is not merged away too early.

## Thermodynamics

- Carnot limit sets max efficiency
- Heat pump COP 300-500 percent

### Entropy

Second law says entropy never decreases in isolated system, a fundamental limit on engines.

## Electricity

Voltage current power, water analogy pressure flow.
"""


def test_markdown_chunks_start_clean():
    chunks = rag._chunk_markdown(MD_SAMPLE)
    assert chunks
    for c in chunks:
        first = c.lstrip()[0]
        # clean start: heading / bullet / capital / digit — never mid-sentence lowercase
        assert first in "#-*•>" or first.isupper() or first.isdigit(), f"bad start: {c[:40]!r}"


def test_markdown_keeps_heading_context():
    chunks = rag._chunk_markdown(MD_SAMPLE)
    joined = "\n---\n".join(chunks)
    # heading text is preserved in the chunks (breadcrumb/section headers kept)
    assert "Thermodynamics" in joined
    assert "Electricity" in joined


def test_headingless_falls_back_to_paragraphs():
    text = "First paragraph here.\n\nSecond paragraph totally separate.\n\nThird one."
    chunks = rag._chunk_markdown(text)
    assert chunks
    assert not any(c.startswith("#") for c in chunks)  # no fake headings


def test_empty_content_returns_empty():
    assert rag._chunk_file("x.md", "") == []
    assert rag._chunk_file("x.md", "   \n\n  ") == []
    assert rag._chunk_markdown("") == []


def test_txt_splits_on_double_newline():
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = rag._chunk_file("note.txt", text)
    assert len(chunks) == 1  # small → merged into one chunk (under MD_MAX_CHUNK)
    assert "Para one" in chunks[0] and "Para three" in chunks[0]


def test_single_paragraph_one_chunk():
    chunks = rag._chunk_file("note.txt", "just one paragraph no breaks")
    assert chunks == ["just one paragraph no breaks"]


def test_oversized_word_is_capped():
    blob = "x" * 5000
    chunks = rag._chunk_file("note.txt", blob)
    assert all(len(c) <= rag.CHUNK_SIZE for c in chunks)


def test_dispatcher_by_extension():
    # .md → heading-aware (headings survive), unknown ext → char-window fallback (no crash)
    assert rag._chunk_file("a.md", MD_SAMPLE)
    assert rag._chunk_file("a.unknown", "some text content here") == ["some text content here"]


# ------------------------------------------------- T2: index / dedup / delete

def _make_messages_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
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
    ])
    return rag.RagMemory(path=tmp_path / "vec.db", msg_db=msg_db)


def _counts(m, file_id=None):
    where = f" WHERE file_id={file_id}" if file_id else ""
    return {
        "files": m.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "vec": m.conn.execute(f"SELECT COUNT(*) FROM vec_files{where}").fetchone()[0],
        "chunks": m.conn.execute(f"SELECT COUNT(*) FROM file_chunks{where}").fetchone()[0],
        "fts": m.conn.execute("SELECT COUNT(*) FROM fts_files").fetchone()[0],
    }


def test_index_file_creates_rows(mem):
    n = mem.index_file("01-daily/dump.md", MD_SAMPLE)
    assert n > 0
    c = _counts(mem)
    assert c["files"] == 1
    assert c["vec"] == c["chunks"] == c["fts"] == n  # one vec/chunk/fts row per chunk


def test_index_file_idempotent_same_content(mem):
    mem.index_file("a.md", MD_SAMPLE)
    fid1 = mem.conn.execute("SELECT file_id FROM files WHERE path='a.md'").fetchone()[0]
    before = _counts(mem)
    n2 = mem.index_file("a.md", MD_SAMPLE)  # same content
    assert n2 == 0
    assert _counts(mem) == before
    fid2 = mem.conn.execute("SELECT file_id FROM files WHERE path='a.md'").fetchone()[0]
    assert fid1 == fid2  # stable file_id


def test_reindex_changed_content_replaces(mem):
    mem.index_file("a.md", "# Old\n\nold content paragraph here that is long enough to chunk.")
    fid = mem.conn.execute("SELECT file_id FROM files WHERE path='a.md'").fetchone()[0]
    old_text = mem.conn.execute(
        "SELECT text FROM file_chunks WHERE file_id=?", (fid,)).fetchone()[0]
    mem.index_file("a.md", MD_SAMPLE)  # different content
    assert mem.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1  # still one file
    new_texts = [r[0] for r in mem.conn.execute(
        "SELECT text FROM file_chunks WHERE file_id=?", (fid,)).fetchall()]
    assert old_text not in new_texts  # old chunks gone
    assert any("Thermodynamics" in t for t in new_texts)


def test_delete_file_removes_all(mem):
    mem.index_file("a.md", MD_SAMPLE)
    assert mem.delete_file("a.md") is True
    c = _counts(mem)
    assert c == {"files": 0, "vec": 0, "chunks": 0, "fts": 0}
    assert mem.delete_file("a.md") is False  # already gone


def test_index_empty_file_skipped(mem):
    assert mem.index_file("empty.md", "   \n\n  ") == 0
    assert _counts(mem)["files"] == 0


def test_files_and_dialogs_independent(mem):
    mem.index_message(1, 100, "user", "я люблю программировать на питоне и пишу backend")
    mem.index_file("note.md", MD_SAMPLE)
    # dialog tables untouched by file indexing
    assert mem.conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] > 0
    assert mem.conn.execute("SELECT COUNT(*) FROM vec_files").fetchone()[0] > 0


# ---------------------------------------- T3: unified search + source attribution

RU_MD = """# Здоровье

## Витамин D

Дефицит витамина D вызывает усталость и снижение иммунитета. Норма 30-50 нг/мл в крови.

## Сон

Глубокий сон важен для восстановления, фазы по 90 минут за ночь.
"""

# большой файл (> MD_MAX_CHUNK) чтобы реально разбился на секции-чанки — проверка chunk-level
_FILLER = ("Дополнительный контекст и детали по теме, растянутые на несколько предложений "
           "для увеличения размера секции до порога разбиения. " * 12)
BIG_MD = f"""# Здоровье

## Витамин D

Дефицит витамина D вызывает усталость и снижение иммунитета. Норма 30-50 нг/мл в крови.
{_FILLER}

## Сон

Глубокий сон важен для восстановления, фазы по 90 минут за ночь.
{_FILLER}
"""


def test_file_result_has_source_and_path(mem):
    # BIG_MD splits into separate section-chunks → matched chunk is a section, not whole file
    assert len(rag._chunk_file("x.md", BIG_MD)) >= 2, "sample must actually split"
    mem.index_file("02-personal/wellness/health.md", BIG_MD)
    res = mem.search(100, "какая норма витамина D в крови", limit=5)
    assert res
    file_hits = [r for r in res if r.get("source") == "file"]
    assert file_hits, f"expected a file hit, got {res}"
    top = file_hits[0]
    assert top["path"] == "02-personal/wellness/health.md"
    assert "витамин" in top["content"].lower()
    # matched CHUNK, not whole file: the Сон section is a separate chunk, not in this one
    assert "Глубокий сон" not in top["content"]


def test_dialog_result_shape_unchanged(mem):
    mem.index_message(1, 100, "user", "я люблю программировать на питоне и пишу backend")
    res = mem.search(100, "какой язык программирования я использую", limit=3)
    assert res
    d = [r for r in res if r.get("source") == "dialog"]
    assert d
    assert {"message_id", "chat_id", "role", "content", "timestamp"} <= set(d[0].keys())


def test_both_sources_can_appear(mem):
    mem.index_message(1, 100, "user", "витамин D я принимаю каждое утро по совету врача")
    mem.index_file("health.md", RU_MD)
    res = mem.search(100, "витамин D дефицит норма", limit=10)
    sources = {r["source"] for r in res}
    assert "file" in sources  # file definitely matches
    # both indexed on the same topic → both retrievable


def test_role_filter_excludes_files(mem):
    mem.index_message(1, 100, "user", "витамин D важен для иммунитета")
    mem.index_file("health.md", RU_MD)
    res = mem.search(100, "витамин D норма", limit=10, role="user")
    assert res
    assert all(r["source"] == "dialog" for r in res), "role filter must exclude files"


def test_rrf_namespaced_keys_no_collision():
    # a dialog message_id and a file chunk_id can be equal ints — must not merge
    fused = rag.RagMemory._rrf([("d", 5)], [("f", 5)])
    assert ("d", 5) in fused and ("f", 5) in fused
    assert len(fused) == 2


def test_dialog_only_corpus_regression(mem):
    # no files indexed → behaves like before, all results are dialog
    mem.index_message(1, 100, "user", "завтра еду на дачу копать картошку весной")
    res = mem.search(100, "поездка на дачу", limit=3)
    assert res
    assert all(r["source"] == "dialog" for r in res)


# ------------------------------------------------------- T5: backfill_files

def _build_knowledge(root: Path):
    (root / "01-daily").mkdir(parents=True)
    (root / "01-daily" / "dump.md").write_text(MD_SAMPLE, encoding="utf-8")
    (root / "note.txt").write_text("plain text note about gardening", encoding="utf-8")
    # excluded dot-dir
    (root / ".claude").mkdir()
    (root / ".claude" / "SKILL.md").write_text("# Tool config\n\nshould be skipped", encoding="utf-8")
    # binary / wrong ext — must be ignored
    (root / "data.json").write_text('{"x": 1}', encoding="utf-8")
    (root / "img.png").write_bytes(b"\x89PNG\x00\x01\x02")


def test_backfill_indexes_md_and_txt(mem, tmp_path):
    kb = tmp_path / "kb"
    _build_knowledge(kb)
    n = mem.backfill_files(kb)
    assert n == 2  # dump.md + note.txt
    paths = {r[0] for r in mem.conn.execute("SELECT path FROM files").fetchall()}
    assert paths == {"01-daily/dump.md", "note.txt"}
    assert not any(".claude" in p for p in paths)  # dot-dir skipped
    assert not any(p.endswith(".json") or p.endswith(".png") for p in paths)  # binaries skipped


def test_backfill_second_run_no_changes(mem, tmp_path):
    kb = tmp_path / "kb"
    _build_knowledge(kb)
    mem.backfill_files(kb)
    n2 = mem.backfill_files(kb)  # nothing changed
    assert n2 == 0


def test_backfill_skips_non_utf8(mem, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "good.md").write_text("# Good\n\nreadable content here", encoding="utf-8")
    (kb / "bad.md").write_bytes(b"\xff\xfe\x00garbage not utf8 \x80\x81")
    n = mem.backfill_files(kb)
    assert n == 1  # only good.md; bad.md skipped, walk not aborted
    paths = {r[0] for r in mem.conn.execute("SELECT path FROM files").fetchall()}
    assert paths == {"good.md"}


def test_backfill_prunes_deleted_files(mem, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    f = kb / "temp.md"
    f.write_text("# Temp\n\nwill be deleted later on disk", encoding="utf-8")
    mem.backfill_files(kb)
    assert mem.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    f.unlink()  # delete on disk while "watcher was down"
    mem.backfill_files(kb)
    assert mem.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0  # pruned


# ---------------------------------------- T6: watcher change→action mapping

def test_file_change_target_filters(tmp_path):
    root = tmp_path
    # indexable
    assert rag.file_change_target(str(root / "a.md"), root) == "a.md"
    assert rag.file_change_target(str(root / "sub" / "b.txt"), root) == "sub/b.txt"
    # wrong extension → None
    assert rag.file_change_target(str(root / "data.json"), root) is None
    assert rag.file_change_target(str(root / "img.png"), root) is None
    # excluded dir → None
    assert rag.file_change_target(str(root / ".claude" / "x.md"), root) is None
    assert rag.file_change_target(str(root / ".git" / "y.md"), root) is None


def test_apply_file_change_index_and_delete(mem, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    f = kb / "live.md"
    f.write_text(MD_SAMPLE, encoding="utf-8")
    # added/modified → index
    n = mem.apply_file_change(False, "live.md", str(f))
    assert n > 0
    assert mem.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    # deleted → remove
    assert mem.apply_file_change(True, "live.md") == 1
    assert mem.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_apply_file_change_missing_file_noop(mem, tmp_path):
    # file vanished before executor ran → skip, no crash
    assert mem.apply_file_change(False, "gone.md", str(tmp_path / "gone.md")) == 0


# ---------------------------------- concurrency: search RO conn vs write (task #10-opt)

def test_readonly_conn_sees_writes_and_searches(tmp_path):
    """RO-инстанс на той же WAL-БД видит записи write-инстанса без реконнекта и ищет —
    доказательство что search не ждёт backfill (отдельный коннект, WAL concurrent read)."""
    msg_db = tmp_path / "messages.db"
    _make_messages_db(msg_db, [(100, "user", "seed")])
    vec = tmp_path / "vec.db"
    w = rag.RagMemory(path=vec, msg_db=msg_db)                 # write instance creates schema
    w.index_file("health.md", RU_MD)
    ro = rag.RagMemory(path=vec, msg_db=msg_db, readonly=True) # RO instance, separate conn
    res = ro.search(100, "витамин D норма в крови", limit=5)
    assert any(r["source"] == "file" and r["path"] == "health.md" for r in res)
    # write MORE after RO opened → RO sees it without reconnect (WAL)
    w.index_file("sleep.md", "# Сон\n\nГлубокий сон и фазы по 90 минут за ночь для восстановления.")
    res2 = ro.search(100, "фазы сна восстановление", limit=5)
    assert any(r.get("path") == "sleep.md" for r in res2)


def test_readonly_conn_cannot_write(tmp_path):
    """RO-коннект не должен писать — fail loud, защита от случайного index на read-потоке."""
    msg_db = tmp_path / "messages.db"
    _make_messages_db(msg_db, [(100, "user", "seed")])
    vec = tmp_path / "vec.db"
    rag.RagMemory(path=vec, msg_db=msg_db)  # create schema
    ro = rag.RagMemory(path=vec, msg_db=msg_db, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.index_file("x.md", RU_MD)


def test_run_routes_search_to_read_executor(monkeypatch):
    """run() маршрутизирует search → read-executor, index → write-executor."""
    calls = []
    monkeypatch.setattr(rag, "_executor", "WRITE")
    monkeypatch.setattr(rag, "_read_executor", "READ")

    async def fake_run_in_executor(ex, fn):
        calls.append(ex)
        return None

    class FakeLoop:
        run_in_executor = staticmethod(fake_run_in_executor)

    import asyncio
    asyncio.run(rag.run(FakeLoop(), "search", 1, "q"))
    asyncio.run(rag.run(FakeLoop(), "index_message", 1, 1, "user", "x"))
    assert calls == ["READ", "WRITE"]
