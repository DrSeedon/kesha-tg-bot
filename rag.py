"""RAG semantic memory — FastEmbed (bge-m3 int8 ONNX) + sqlite-vec hybrid search.

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
# bge-m3 int8 ONNX: separation margin +0.237 vs e5-small +0.055 (4x шире на абстрактных
# русских запросах). Single-file model_quantized.onnx — fp32 с external onnx_data падает в ORT.
# MODEL_NAME ≠ нативному имени FastEmbed, иначе add_custom_model пропустится → fp32 → краш.
MODEL_NAME = "AlpEge/bge-m3-onnx-int8"
MODEL_HF = "AlpEge/bge-m3-onnx-int8"
MODEL_FILE = "model_quantized.onnx"
DIM = 1024
MODEL_PREFIX = False  # bge-m3: CLS-пулинг, без query:/passage: префиксов. E5-модели → True.
MODEL_POOLING = "cls"  # bge-m3 = CLS. E5 = mean.
RRF_K = 60
# bump при ЛЮБОМ изменении схемы vec/fts → старые таблицы дропаются и ребилдятся из messages.db.
# v7: e5-small→bge-m3, dim 384→1024. индекс производный, дроп безопасен. messages.db не трогается.
# v8: +файловые таблицы (vec_files/file_chunks/fts_files/files) для индексации cog-second-brain.
SCHEMA_VERSION = 8
POOL_MULT = 4  # candidate pool = limit * POOL_MULT перед RRF

# Chunking длинных сообщений (голосовые на 500 слов размывают семантику в 1 вектор).
# В символах (~4 символа/токен рус.), без tiktoken. message_id*CHUNK_STRIDE+idx = chunk_id.
CHUNK_CHAR_LIMIT = 1200   # ~300 токенов — выше этого режем
CHUNK_SIZE = 800          # ~200 токенов на кусок
CHUNK_OVERLAP = 200       # ~50 токенов перекрытие
CHUNK_STRIDE = 1000       # макс чанков на сообщение (chunk_id = parent*STRIDE + idx)

# Файловая индексация базы знаний (cog-second-brain). Только текст-проза: .md/.txt.
# xml/csv/json/html = машинные данные/логи → мусор в retrieval (замер task #10).
# KNOWLEDGE_DIR = корень базы знаний (WORK_DIR бота = /opt/cog-second-brain на проде).
import os
KNOWLEDGE_DIR = Path(os.getenv("WORK_DIR", ".")).resolve()
FILE_EXTENSIONS = {".md", ".txt"}
EXCLUDED_DIRS = {".git", ".claude", ".gemini", ".kiro", ".github", ".serena",
                 ".claude-plugin", "node_modules", "__pycache__"}
# md heading-aware: секция > MD_MAX режем по параграфам, секция < MD_MIN мержим с соседней.
# Замер (task #10): multi-doc top3 5/5 vs naive 4/5, start-clean 100% vs 33%.
MD_MAX_CHUNK = 1500
MD_MIN_MERGE = 250
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


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


def file_change_target(abs_path: str, root: Path | None = None) -> str | None:
    """Абсолютный путь → относительный (от KNOWLEDGE_DIR) если файл подлежит индексации,
    иначе None. Фильтрует по расширению и EXCLUDED_DIRS. Чистая функция для watcher/тестов."""
    root = (root or KNOWLEDGE_DIR).resolve()
    p = Path(abs_path)
    if p.suffix.lower() not in FILE_EXTENSIONS:
        return None
    try:
        rel = p.resolve().relative_to(root)
    except ValueError:
        return None  # вне базы знаний
    if any(part in EXCLUDED_DIRS for part in rel.parts):
        return None
    return str(rel)


def _split_paragraphs(text: str, max_chunk: int) -> list[str]:
    """Режем по двойному \\n, набирая параграфы до max_chunk. Параграф длиннее max_chunk сам
    по себе → отдаём как есть (char-cap применит _chunk выше по стеку)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > max_chunk:
            chunks.append(cur.strip())
            cur = ""
        cur = (cur + "\n\n" + p) if cur else p
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _chunk_markdown(content: str) -> list[str]:
    """Heading-aware: секция под каждым заголовком = кусок, с хлебной крошкой (Physics > Thermo).
    Большая секция → делим по параграфам, крошечная → копим с соседней. Без заголовков → параграфы.
    Крошка даёт контекст изолированному чанку (иначе '- КПД 300%' без темы)."""
    if not content or not content.strip():
        return []
    lines = content.split("\n")
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    crumb = ""

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append((crumb, text))

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush()
            buf.clear()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            crumb = " > ".join(t for _, t in stack)
        buf.append(line)
    flush()

    if not sections:  # без заголовков (дневники) → параграфы, фолбэк на char-window
        return _split_paragraphs(content, MD_MAX_CHUNK) or _chunk(content)

    chunks: list[str] = []
    pending = ""
    for crumb, text in sections:
        # крошка-контекст: если у секции есть заголовки-предки, префиксуем (кроме случая когда
        # текст уже начинается с этого заголовка — первая строка секции = сам heading)
        block = text
        if len(text) > MD_MAX_CHUNK:
            if pending:
                chunks.append(pending.strip())
                pending = ""
            chunks.extend(_split_paragraphs(block, MD_MAX_CHUNK))
            continue
        if pending and len(pending) + len(block) + 2 > MD_MAX_CHUNK:
            chunks.append(pending.strip())
            pending = block
        else:
            pending = (pending + "\n\n" + block) if pending else block
        if len(pending) >= MD_MIN_MERGE:
            chunks.append(pending.strip())
            pending = ""
    if pending.strip():
        chunks.append(pending.strip())
    return chunks[:CHUNK_STRIDE - 1]


def _chunk_file(path: str, content: str) -> list[str]:
    """Диспетчер по расширению. .md → heading-aware, .txt → параграфы, иначе char-window.
    Пустой/пробельный content → []."""
    if not content or not content.strip():
        return []
    ext = Path(path).suffix.lower()
    if ext == ".md":
        chunks = _chunk_markdown(content)
    elif ext == ".txt":
        chunks = _split_paragraphs(content, MD_MAX_CHUNK) or _chunk(content)
    else:
        chunks = _chunk(content)
    # финальный char-cap: параграф длиннее MD_MAX не должен уйти гигантским вектором
    out: list[str] = []
    for c in chunks:
        out.extend(_chunk(c) if len(c) > CHUNK_CHAR_LIMIT else [c])
    return out[:CHUNK_STRIDE - 1]


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
            self.conn.execute("DROP TABLE IF EXISTS vec_files")
            self.conn.execute("DROP TABLE IF EXISTS fts_files")
            self.conn.execute("DROP TABLE IF EXISTS file_chunks")
            self.conn.execute("DROP TABLE IF EXISTS files")
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
        # --- файловые таблицы (task #10). Отдельно от диалоговых → диалоговый RAG не трогаем.
        # files: метаданные + дедуп (sha256 по контенту, path относительный от KNOWLEDGE_DIR).
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                sha256 TEXT NOT NULL,
                mtime REAL NOT NULL
            )
        """)
        # vec_files: БЕЗ chat-партиции (файлы общие для всех юзеров). chunk_id = file_id*STRIDE+idx.
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_files USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                file_id INTEGER,
                embedding FLOAT[{DIM}]
            )
        """)
        # file_chunks: хранилище текста чанка для read-path (файлов нет в messages.db).
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS file_chunks (
                chunk_id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                text TEXT NOT NULL
            )
        """)
        # fts_files: rowid = chunk_id → matched CHUNK идентифицируем + O(1) delete по rowid
        # (DELETE WHERE unindexed-col работает, но full-scan; rowid-delete идиоматичнее).
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_files USING fts5(text)
        """)

    def _embed(self, texts: list[str], is_query: bool) -> list[list[float]]:
        if self._embedder is None:
            import onnxruntime as _ort
            _orig_sess = _ort.InferenceSession.__init__
            def _patched_init(self_sess, *a, **k):
                so = k.get("sess_options") or _ort.SessionOptions()
                so.enable_cpu_mem_arena = False
                so.enable_mem_pattern = False
                k["sess_options"] = so
                _orig_sess(self_sess, *a, **k)
            _ort.InferenceSession.__init__ = _patched_init
            from fastembed import TextEmbedding
            from fastembed.common.model_description import PoolingType, ModelSource
            if MODEL_NAME not in {m["model"] for m in TextEmbedding.list_supported_models()}:
                pooling = PoolingType.CLS if MODEL_POOLING == "cls" else PoolingType.MEAN
                TextEmbedding.add_custom_model(
                    model=MODEL_NAME, pooling=pooling, normalization=True,
                    sources=ModelSource(hf=MODEL_HF), dim=DIM, model_file=MODEL_FILE,
                )
            self._embedder = TextEmbedding(model_name=MODEL_NAME)
            logger.info(f"RAG embedder loaded: {MODEL_NAME}")
        # E5 models need "query: "/"passage: " prefix; bge-m3 doesn't. Explicit flag > name-sniffing.
        if MODEL_PREFIX:
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

    # ------------------------------------------------------------ файлы (task #10)

    @staticmethod
    def _sha256(content: str) -> str:
        import hashlib
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _delete_file_rows(self, file_id: int) -> None:
        """Удалить все чанки file_id из vec/fts/file_chunks. Вызывать внутри транзакции."""
        rows = self.conn.execute(
            "SELECT chunk_id FROM file_chunks WHERE file_id=?", (file_id,)
        ).fetchall()
        for r in rows:
            cid = r["chunk_id"]
            self.conn.execute("DELETE FROM vec_files WHERE chunk_id=?", (cid,))
            self.conn.execute("DELETE FROM fts_files WHERE rowid=?", (cid,))
        self.conn.execute("DELETE FROM file_chunks WHERE file_id=?", (file_id,))

    def index_file(self, rel_path: str, content: str, mtime: float = 0.0) -> int:
        """Индексирует файл. Дедуп по sha256: тот же контент → no-op. Изменился → удаляем
        старые чанки + переиндексируем. Возвращает число проиндексированных чанков (0 = skip)."""
        chunks = _chunk_file(rel_path, content)
        if not chunks:  # пустой/пробельный файл
            return 0
        sha = self._sha256(content)
        existing = self.conn.execute(
            "SELECT file_id, sha256 FROM files WHERE path=?", (rel_path,)
        ).fetchone()
        if existing and existing["sha256"] == sha:
            return 0  # контент не изменился
        vecs = self._embed(chunks, is_query=False)
        self.conn.execute("BEGIN")
        try:
            if existing:
                file_id = existing["file_id"]
                self._delete_file_rows(file_id)
                self.conn.execute("UPDATE files SET sha256=?, mtime=? WHERE file_id=?",
                                  (sha, mtime, file_id))
            else:
                cur = self.conn.execute(
                    "INSERT INTO files(path, sha256, mtime) VALUES(?,?,?)", (rel_path, sha, mtime))
                file_id = int(cur.lastrowid)
            for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
                cid = file_id * CHUNK_STRIDE + idx
                self.conn.execute(
                    "INSERT INTO vec_files(chunk_id, file_id, embedding) VALUES(?,?,?)",
                    (cid, file_id, _pack(vec)))
                self.conn.execute(
                    "INSERT INTO file_chunks(chunk_id, file_id, text) VALUES(?,?,?)",
                    (cid, file_id, chunk))
                self.conn.execute(
                    "INSERT INTO fts_files(rowid, text) VALUES(?,?)", (cid, chunk))
            self.conn.execute("COMMIT")
            return len(chunks)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def apply_file_change(self, deleted: bool, rel_path: str, abs_path: str = "") -> int:
        """Watcher-хук: deleted → delete_file; иначе читаем abs_path и index_file.
        Вызывается в executor-потоке. Не-UTF8/пропавший файл → тихо skip (0)."""
        if deleted:
            return 1 if self.delete_file(rel_path) else 0
        try:
            content = Path(abs_path).read_text(encoding="utf-8")
            mtime = Path(abs_path).stat().st_mtime
        except (UnicodeDecodeError, OSError):
            return 0
        return self.index_file(rel_path, content, mtime)

    def delete_file(self, rel_path: str) -> bool:
        """Удаляет все чанки файла по пути. True если файл был проиндексирован."""
        row = self.conn.execute("SELECT file_id FROM files WHERE path=?", (rel_path,)).fetchone()
        if not row:
            return False
        self.conn.execute("BEGIN")
        try:
            self._delete_file_rows(row["file_id"])
            self.conn.execute("DELETE FROM files WHERE file_id=?", (row["file_id"],))
            self.conn.execute("COMMIT")
            return True
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

    def _vec_search_files(self, query_vec: list[float], pool: int) -> list[int]:
        # chunk-level: НЕ дедупим до file_id (Codex fix) — matched секция и есть результат.
        # без chat-партиции: файлы общие. pool*3 headroom как у диалогов.
        rows = self.conn.execute(
            "SELECT chunk_id FROM vec_files WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_pack(query_vec), pool * 3)).fetchall()
        return [r["chunk_id"] for r in rows][:pool]

    def _fts_search_files(self, query: str, pool: int) -> list[int]:
        sql = "SELECT rowid AS chunk_id FROM fts_files WHERE fts_files MATCH ? ORDER BY rank LIMIT ?"
        match = self._expand_query(query) or ('"' + query.replace('"', '""') + '"')
        try:
            rows = self.conn.execute(sql, (match, pool * 3)).fetchall()
        except Exception:
            safe = '"' + query.replace('"', '""') + '"'
            rows = self.conn.execute(sql, (safe, pool * 3)).fetchall()
        return [r["chunk_id"] for r in rows][:pool]

    @staticmethod
    def _rrf(*ranked_lists: list, k: int = RRF_K) -> list:
        """RRF над произвольным числом ранжированных списков. Ключи — любые hashable
        (namespaced ('d',msg_id)/('f',chunk_id) → диалог и файл с равными int не схлопнутся)."""
        scores: dict = {}
        for lst in ranked_lists:
            for rank, key in enumerate(lst):
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        return sorted(scores, key=lambda m: scores[m], reverse=True)

    def search(self, chat_id: int, query: str, limit: int = 5, role: str | None = None,
               before: str | None = None, after: str | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        pool = max(limit * POOL_MULT, limit)
        qvec = self._embed([query], is_query=True)[0]
        # диалоги: namespaced ключ ('d', parent_message_id)
        d_vec = [("d", m) for m in self._vec_search(chat_id, qvec, pool, role)]
        d_fts = [("d", m) for m in self._fts_search(chat_id, query, pool, role)]
        # файлы: chunk-level, ключ ('f', chunk_id). role-фильтр → диалоги-only (у файлов нет role).
        if role:
            f_vec, f_fts = [], []
        else:
            f_vec = [("f", c) for c in self._vec_search_files(qvec, pool)]
            f_fts = [("f", c) for c in self._fts_search_files(query, pool)]
        ranked = self._rrf(d_vec, d_fts, f_vec, f_fts)
        if not ranked:
            return []

        msg_ids = [key[1] for key in ranked if key[0] == "d"]
        chunk_ids = [key[1] for key in ranked if key[0] == "f"]
        dialog_rows: dict = {}
        if msg_ids:
            ph = ",".join("?" * len(msg_ids))
            sql = (f"SELECT id AS message_id, chat_id, role, content, timestamp "
                   f"FROM msg.messages WHERE id IN ({ph})")
            params: list = list(msg_ids)
            if role:
                sql += " AND role=?"
                params.append(role)
            if after:
                sql += " AND timestamp>=?"
                params.append(after)
            if before:
                sql += " AND timestamp<=?"
                params.append(before)
            dialog_rows = {r["message_id"]: {**dict(r), "source": "dialog"}
                           for r in self.conn.execute(sql, params).fetchall()}
        file_rows: dict = {}
        if chunk_ids:
            ph = ",".join("?" * len(chunk_ids))
            sql = (f"SELECT fc.chunk_id, fc.text AS content, f.path "
                   f"FROM file_chunks fc JOIN files f ON f.file_id=fc.file_id "
                   f"WHERE fc.chunk_id IN ({ph})")
            file_rows = {r["chunk_id"]: {"source": "file", "path": r["path"],
                                         "content": r["content"]}
                         for r in self.conn.execute(sql, list(chunk_ids)).fetchall()}
        # вернуть в порядке RRF, до limit
        out = []
        for kind, key in ranked:
            row = dialog_rows.get(key) if kind == "d" else file_rows.get(key)
            if row:
                out.append(row)
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

    def _walk_knowledge(self, root: Path) -> list[Path]:
        """Все .md/.txt под root, пропуская EXCLUDED_DIRS. Возвращает абсолютные пути."""
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
            for fn in filenames:
                if Path(fn).suffix.lower() in FILE_EXTENSIONS:
                    found.append(Path(dirpath) / fn)
        return found

    def backfill_files(self, root: Path | None = None) -> int:
        """Индексирует все .md/.txt из базы знаний. Дедуп по sha256 → повторный запуск дёшев.
        Не-UTF8 файл → skip+log (не рушим обход). Prune: файлы из `files` которых нет на диске
        → удаляем (out-of-band удаление пока watcher был выключен). Возвращает число (ре)индексаций."""
        root = (root or KNOWLEDGE_DIR).resolve()
        if not root.is_dir():
            logger.warning(f"RAG backfill_files: knowledge dir not found: {root}")
            return 0
        disk_paths = self._walk_knowledge(root)
        seen_rel: set[str] = set()
        count = 0
        for abs_path in disk_paths:
            rel = str(abs_path.relative_to(root))
            seen_rel.add(rel)
            try:
                content = abs_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning(f"RAG backfill_files: skip non-UTF8 {rel}")
                continue
            except OSError as e:
                logger.warning(f"RAG backfill_files: skip {rel}: {e}")
                continue
            try:
                mtime = abs_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if self.index_file(rel, content, mtime):
                count += 1
        # prune: индексированные пути, которых больше нет на диске
        indexed_paths = [r["path"] for r in self.conn.execute("SELECT path FROM files").fetchall()]
        for rel in indexed_paths:
            if rel not in seen_rel:
                self.delete_file(rel)
                logger.info(f"RAG backfill_files: pruned stale {rel}")
        if count:
            logger.info(f"RAG backfill_files indexed/updated {count} files")
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
