"""RAG semantic memory — FastEmbed (multilingual MiniLM) + sqlite-vec hybrid search.

ВСЕ методы RagMemory вызываются ТОЛЬКО из единого rag_executor (ThreadPoolExecutor
max_workers=1). Коннект sqlite и embedder привязаны к этому потоку — не дёргать из
других потоков (SQLite не thread-safe). См. docs/tasks/rag-memory/plan.md.
"""

import logging
import struct
from pathlib import Path

import sqlite_vec

logger = logging.getLogger("kesha.rag")

DB_PATH = Path("./storage/vec.db")
MSG_DB_PATH = Path("./storage/messages.db")
# planned mE5-small отсутствует в FastEmbed по имени (ONNX не по ожидаемому пути в HF);
# MiniLM multilingual — нативно в FastEmbed, 384 dims, ~220MB, без torch. См. plan.md Phase 3.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384
RRF_K = 60
# bump при ЛЮБОМ изменении схемы vec/fts → старые таблицы дропаются и ребилдятся из messages.db.
# индекс — производная (backfill восстановит), дроп безопасен. Также страхует от alpha-формата sqlite-vec.
SCHEMA_VERSION = 1
POOL_MULT = 4  # candidate pool = limit * POOL_MULT перед RRF


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class RagMemory:
    def __init__(self, path: Path = DB_PATH, msg_db: Path = MSG_DB_PATH):
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=True (default) — мы всегда в одном executor-потоке
        # uri=True — нужно для ATTACH '...?mode=ro' (file: URI синтаксис)
        self.conn = sqlite3.connect(str(path), isolation_level=None, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # ATTACH messages.db read-only для джойна content/timestamp
        self.conn.execute(f"ATTACH DATABASE 'file:{msg_db}?mode=ro' AS msg")
        self._create_schema()
        self._embedder = None

    def _create_schema(self) -> None:
        # схема изменилась (или alpha-формат sqlite-vec) → дроп + ребилд из messages.db.
        # CREATE ... IF NOT EXISTS НЕ мигрирует существующую таблицу — поэтому версионируем.
        ver = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if ver != SCHEMA_VERSION:
            self.conn.execute("DROP TABLE IF EXISTS vec_messages")
            self.conn.execute("DROP TABLE IF EXISTS fts_messages")
            self.conn.execute("DROP TABLE IF EXISTS indexed")
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            if ver != 0:
                logger.info(f"RAG schema v{ver}→v{SCHEMA_VERSION}: dropped index, will rebuild via backfill")
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
                message_id INTEGER PRIMARY KEY,
                chat_id INTEGER PARTITION KEY,
                role TEXT,
                embedding FLOAT[{DIM}]
            )
        """)
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
                content, chat_id UNINDEXED, role UNINDEXED, message_id UNINDEXED
            )
        """)
        self.conn.execute("CREATE TABLE IF NOT EXISTS indexed (message_id INTEGER PRIMARY KEY)")

    def _embed(self, texts: list[str], is_query: bool) -> list[list[float]]:
        if self._embedder is None:
            from fastembed import TextEmbedding

            self._embedder = TextEmbedding(model_name=MODEL_NAME)
            logger.info(f"RAG embedder loaded: {MODEL_NAME}")
        prefix = "query: " if is_query else "passage: "
        prefixed = [prefix + t for t in texts]
        return [list(map(float, v)) for v in self._embedder.embed(prefixed)]

    def _is_indexed(self, message_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM indexed WHERE message_id=?", (message_id,)
        ).fetchone() is not None

    def index_message(self, message_id: int, chat_id: int, role: str, content: str) -> None:
        if role == "system" or not content or not content.strip():
            return
        if self._is_indexed(message_id):
            return
        vec = self._embed([content], is_query=False)[0]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO vec_messages(message_id, chat_id, role, embedding) VALUES(?,?,?,?)",
                (message_id, chat_id, role, _pack(vec)),
            )
            self.conn.execute(
                "INSERT INTO fts_messages(content, chat_id, role, message_id) VALUES(?,?,?,?)",
                (content, chat_id, role, message_id),
            )
            self.conn.execute("INSERT INTO indexed(message_id) VALUES(?)", (message_id,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _vec_search(self, chat_id: int, query_vec: list[float], pool: int, role: str | None) -> list[int]:
        sql = "SELECT message_id FROM vec_messages WHERE chat_id=? AND embedding MATCH ? "
        params: list = [chat_id, _pack(query_vec)]
        if role:
            sql += "AND role=? "
            params.append(role)
        sql += "ORDER BY distance LIMIT ?"
        params.append(pool)
        return [r["message_id"] for r in self.conn.execute(sql, params).fetchall()]

    def _fts_search(self, chat_id: int, query: str, pool: int, role: str | None) -> list[int]:
        role_sql = " AND role=?" if role else ""
        sql = (f"SELECT message_id FROM fts_messages WHERE fts_messages MATCH ? AND chat_id=?{role_sql} "
               f"ORDER BY rank LIMIT ?")

        def _params(q):
            p: list = [q, chat_id]
            if role:
                p.append(role)
            p.append(pool)
            return p
        try:
            rows = self.conn.execute(sql, _params(query)).fetchall()
        except Exception:
            # FTS5 MATCH синтаксис чувствителен к спецсимволам — фолбэк на phrase-quote
            safe = '"' + query.replace('"', '""') + '"'
            rows = self.conn.execute(sql, _params(safe)).fetchall()
        return [r["message_id"] for r in rows]

    @staticmethod
    def _rrf(vec_ranked: list[int], fts_ranked: list[int], k: int = RRF_K) -> list[int]:
        scores: dict[int, float] = {}
        for rank, mid in enumerate(vec_ranked):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
        for rank, mid in enumerate(fts_ranked):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
        return sorted(scores, key=lambda m: scores[m], reverse=True)

    def search(self, chat_id: int, query: str, limit: int = 5, role: str | None = None,
               before: str | None = None, after: str | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        pool = max(limit * POOL_MULT, limit)
        qvec = self._embed([query], is_query=True)[0]
        vec_ids = self._vec_search(chat_id, qvec, pool, role)
        fts_ids = self._fts_search(chat_id, query, pool, role)
        ranked = self._rrf(vec_ids, fts_ids)
        if not ranked:
            return []
        # джойн к messages.db за content+timestamp, фильтр по дате/роли
        placeholders = ",".join("?" * len(ranked))
        sql = (f"SELECT id AS message_id, chat_id, role, content, timestamp "
               f"FROM msg.messages WHERE id IN ({placeholders})")
        params: list = list(ranked)
        if role:
            sql += " AND role=?"
            params.append(role)
        if after:
            sql += " AND timestamp>=?"
            params.append(after)
        if before:
            sql += " AND timestamp<=?"
            params.append(before)
        rows = {r["message_id"]: dict(r) for r in self.conn.execute(sql, params).fetchall()}
        # вернуть в порядке RRF, до limit
        out = []
        for mid in ranked:
            if mid in rows:
                out.append(rows[mid])
                if len(out) >= limit:
                    break
        return out

    def backfill(self, batch_size: int = 64) -> int:
        """Чанками: каждый запрос берёт следующий batch неиндексированных (WHERE NOT IN indexed),
        embed+insert, commit. Не грузит всю историю в память (100K на VPS 3.5GB)."""
        count = 0
        while True:
            batch = self.conn.execute("""
                SELECT m.id, m.chat_id, m.role, m.content
                FROM msg.messages m
                WHERE m.role != 'system'
                  AND trim(m.content) != ''
                  AND m.id NOT IN (SELECT message_id FROM indexed)
                ORDER BY m.id LIMIT ?
            """, (batch_size,)).fetchall()
            if not batch:
                break
            vecs = self._embed([r["content"] for r in batch], is_query=False)
            self.conn.execute("BEGIN")
            try:
                for r, vec in zip(batch, vecs):
                    self.conn.execute(
                        "INSERT INTO vec_messages(message_id, chat_id, role, embedding) VALUES(?,?,?,?)",
                        (r["id"], r["chat_id"], r["role"], _pack(vec)),
                    )
                    self.conn.execute(
                        "INSERT INTO fts_messages(content, chat_id, role, message_id) VALUES(?,?,?,?)",
                        (r["content"], r["chat_id"], r["role"], r["id"]),
                    )
                    self.conn.execute("INSERT INTO indexed(message_id) VALUES(?)", (r["id"],))
                self.conn.execute("COMMIT")
                count += len(batch)
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        if count:
            logger.info(f"RAG backfill indexed {count} messages")
        return count


_db: RagMemory | None = None
_executor = None  # ThreadPoolExecutor(max_workers=1), set from bot.py


def set_executor(ex) -> None:
    global _executor
    _executor = ex


def get_rag() -> RagMemory:
    """Lazy singleton. ВЫЗЫВАТЬ ТОЛЬКО внутри executor-потока (первый вызов
    создаёт коннект+embedder, которые привязываются к текущему потоку)."""
    global _db
    if _db is None:
        _db = RagMemory()
    return _db


async def run(loop, method: str, *args):
    """Выполнить RagMemory.<method>(*args) в executor-потоке. get_rag() вызывается
    ВНУТРИ executor — иначе коннект привяжется к loop-потоку (SQLite check_same_thread)."""
    def _call():
        return getattr(get_rag(), method)(*args)
    return await loop.run_in_executor(_executor, _call)
