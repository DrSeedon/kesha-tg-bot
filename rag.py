"""RAG semantic memory — FastEmbed (multilingual-e5-base) + sqlite-vec hybrid search.

ВСЕ методы RagMemory вызываются ТОЛЬКО из единого rag_executor (ThreadPoolExecutor
max_workers=1). Коннект sqlite и embedder привязаны к этому потоку — не дёргать из
других потоков (SQLite не thread-safe). См. docs/tasks/rag-memory/plan.md.
"""

import logging
import re
import struct
from pathlib import Path

import sqlite_vec

logger = logging.getLogger("kesha.rag")

DB_PATH = Path("./storage/vec.db")
MSG_DB_PATH = Path("./storage/messages.db")
# MiniLM: стабильно работает на VPS 2.9GB RAM (~220MB). mpnet и e5-large OOM'или.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_FILE = None
DIM = 384
RRF_K = 60
# bump при ЛЮБОМ изменении схемы vec/fts → старые таблицы дропаются и ребилдятся из messages.db.
# v2: dim 384→1024 + parent_message_id (chunking). индекс производный, дроп безопасен.
SCHEMA_VERSION = 5
POOL_MULT = 4  # candidate pool = limit * POOL_MULT перед RRF

# Chunking длинных сообщений (голосовые на 500 слов размывают семантику в 1 вектор).
# В символах (~4 символа/токен рус.), без tiktoken. message_id*CHUNK_STRIDE+idx = chunk_id.
CHUNK_CHAR_LIMIT = 1200   # ~300 токенов — выше этого режем
CHUNK_SIZE = 800          # ~200 токенов на кусок
CHUNK_OVERLAP = 200       # ~50 токенов перекрытие
CHUNK_STRIDE = 1000       # макс чанков на сообщение (chunk_id = parent*STRIDE + idx)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _split_oversized(words: list[str]) -> list[str]:
    """Слово длиннее CHUNK_SIZE (URL/base64/blob) → режем char-окном, иначе обходит лимит."""
    out = []
    for w in words:
        if len(w) > CHUNK_SIZE:
            out.extend(w[i:i + CHUNK_SIZE] for i in range(0, len(w), CHUNK_SIZE))
        else:
            out.append(w)
    return out


def _chunk(content: str) -> list[str]:
    """Длинный content → куски ~CHUNK_SIZE символов с overlap. Короткий → [content].
    Жёсткий cap CHUNK_STRIDE-1 чанков (chunk_id = parent*STRIDE+idx не должен пересечь следующий parent)."""
    if len(content) <= CHUNK_CHAR_LIMIT:
        return [content]
    words = _split_oversized(content.split())
    chunks, cur, cur_len = [], [], 0
    for w in words:
        cur.append(w)
        cur_len += len(w) + 1
        if cur_len >= CHUNK_SIZE:
            chunks.append(" ".join(cur))
            # overlap: оставить хвост слов на ~CHUNK_OVERLAP символов.
            # слово длиннее остатка бюджета НЕ берём (иначе chunk раздувается > CHUNK_SIZE).
            keep, klen = [], 0
            for tw in reversed(cur):
                if klen + len(tw) + 1 > CHUNK_OVERLAP:
                    break
                keep.insert(0, tw)
                klen += len(tw) + 1
            cur, cur_len = keep, klen
    if cur and (not chunks or " ".join(cur) != chunks[-1]):
        chunks.append(" ".join(cur))
    # cap: при экстремально длинном тексте не дать idx достичь CHUNK_STRIDE (иначе chunk_id collision)
    return chunks[:CHUNK_STRIDE - 1]


def _dedup(ids: list[int]) -> list[int]:
    """Уникальные с сохранением порядка (лучшего ранга). Чанки одного сообщения → один parent."""
    seen: set = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


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
                chunk_id INTEGER PRIMARY KEY,
                parent_message_id INTEGER,
                chat_id INTEGER PARTITION KEY,
                role TEXT,
                embedding FLOAT[{DIM}]
            )
        """)
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
                content, chat_id UNINDEXED, role UNINDEXED, parent_message_id UNINDEXED
            )
        """)
        self.conn.execute("CREATE TABLE IF NOT EXISTS indexed (message_id INTEGER PRIMARY KEY)")

    def _embed(self, texts: list[str], is_query: bool) -> list[list[float]]:
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=MODEL_NAME)
            logger.info(f"RAG embedder loaded: {MODEL_NAME}")
        # E5 models need "query: "/"passage: " prefix, mpnet/MiniLM don't
        if "e5" in MODEL_NAME:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
        return [list(map(float, v)) for v in self._embedder.embed(texts, batch_size=16)]

    def _is_indexed(self, message_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM indexed WHERE message_id=?", (message_id,)
        ).fetchone() is not None

    def index_message(self, message_id: int, chat_id: int, role: str, content: str) -> None:
        if role == "system" or not content or not content.strip():
            return
        if self._is_indexed(message_id):
            return
        chunks = _chunk(content)
        vecs = self._embed(chunks, is_query=False)
        self.conn.execute("BEGIN")
        try:
            for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
                self.conn.execute(
                    "INSERT INTO vec_messages(chunk_id, parent_message_id, chat_id, role, embedding) "
                    "VALUES(?,?,?,?,?)",
                    (message_id * CHUNK_STRIDE + idx, message_id, chat_id, role, _pack(vec)),
                )
                self.conn.execute(
                    "INSERT INTO fts_messages(content, chat_id, role, parent_message_id) VALUES(?,?,?,?)",
                    (chunk, chat_id, role, message_id),
                )
            self.conn.execute("INSERT INTO indexed(message_id) VALUES(?)", (message_id,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _vec_search(self, chat_id: int, query_vec: list[float], pool: int, role: str | None) -> list[int]:
        # pool*3 headroom: чанки одного сообщения схлопнутся в один parent при дедупе.
        # на 2 юзера/limit=5 (pool=20→60 кандидатов) хватает уникальных parent с запасом.
        sql = "SELECT parent_message_id FROM vec_messages WHERE chat_id=? AND embedding MATCH ? "
        params: list = [chat_id, _pack(query_vec)]
        if role:
            sql += "AND role=? "
            params.append(role)
        sql += "ORDER BY distance LIMIT ?"
        params.append(pool * 3)
        return _dedup([r["parent_message_id"] for r in self.conn.execute(sql, params).fetchall()])[:pool]

    @staticmethod
    def _expand_query(query: str) -> str | None:
        """prefix-expansion для русской морфологии: 'ссора Катей' → '\"ссора\"* OR \"Катей\"*'.
        Ловит суффиксальные словоформы (расст*→расстаться/расставание). Слова <3 символов отбрасываем."""
        words = [w for w in re.findall(r"\w+", query) if len(w) >= 3]
        if not words:
            return None
        return " OR ".join(f'"{w}"*' for w in words)

    def _fts_search(self, chat_id: int, query: str, pool: int, role: str | None) -> list[int]:
        role_sql = " AND role=?" if role else ""
        sql = (f"SELECT parent_message_id FROM fts_messages WHERE fts_messages MATCH ? AND chat_id=?{role_sql} "
               f"ORDER BY rank LIMIT ?")

        def _params(q):
            p: list = [q, chat_id]
            if role:
                p.append(role)
            p.append(pool * 3)
            return p
        match = self._expand_query(query) or ('"' + query.replace('"', '""') + '"')
        try:
            rows = self.conn.execute(sql, _params(match)).fetchall()
        except Exception:
            # FTS5 MATCH синтаксис чувствителен к спецсимволам — фолбэк на phrase-quote
            safe = '"' + query.replace('"', '""') + '"'
            rows = self.conn.execute(sql, _params(safe)).fetchall()
        return _dedup([r["parent_message_id"] for r in rows])[:pool]

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
            # чанкуем каждое сообщение, embed плоский список всех чанков батча одним вызовом
            per_msg = [(r, _chunk(r["content"])) for r in batch]
            flat = [c for _, chunks in per_msg for c in chunks]
            vecs = self._embed(flat, is_query=False)
            self.conn.execute("BEGIN")
            try:
                vi = 0
                for r, chunks in per_msg:
                    for idx, chunk in enumerate(chunks):
                        self.conn.execute(
                            "INSERT INTO vec_messages(chunk_id, parent_message_id, chat_id, role, embedding) "
                            "VALUES(?,?,?,?,?)",
                            (r["id"] * CHUNK_STRIDE + idx, r["id"], r["chat_id"], r["role"], _pack(vecs[vi])),
                        )
                        self.conn.execute(
                            "INSERT INTO fts_messages(content, chat_id, role, parent_message_id) VALUES(?,?,?,?)",
                            (chunk, r["chat_id"], r["role"], r["id"]),
                        )
                        vi += 1
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
