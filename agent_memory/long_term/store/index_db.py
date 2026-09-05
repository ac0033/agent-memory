"""SQLite 派生索引（data/index.db）：sqlite-vec 向量 + FTS5 全文。

这是 data/memory Markdown 层的派生物，必须能通过 rebuild_from_markdown 全量重建，
绝不手改（红线 D1）。三张表：
- memories_meta：条目元数据（id 主键 + scope/confidence/日期等过滤字段）；
- memories_vec：vec0 虚表，bge-m3 1024 维，cosine 距离，memory_id 作辅助列；
- memories_fts：FTS5 全文，trigram 分词器（默认 unicode61 分词不支持中文子串匹配）。
"""

import hashlib
import json
import math
import sqlite3
import struct
import threading
from datetime import date
from pathlib import Path

import sqlite_vec

from agent_memory.long_term.store.markdown_store import entry_from_markdown
from agent_memory.models import MemoryEntry

# bge-m3 的向量维度
EMBEDDING_DIM = 1024


class IndexDB:
    """data/index.db 的读写封装。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：MCP server 在 worker 线程里执行 tool handler，
        # 而连接在主线程创建；reconcile 第一阶段还会并发检索。sqlite3 连接本身
        # 不支持并发调用（threadsafety=1），所以所有连接访问经 _lock 串行化
        #（RLock：upsert 内部会调 delete）
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS memories_meta (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                confidence TEXT NOT NULL,
                last_verified TEXT NOT NULL,
                created_at TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '',
                vector_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0 (
                embedding float[{EMBEDDING_DIM}] distance_metric=cosine,
                +memory_id TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5 (
                id UNINDEXED,
                content,
                tokenize='trigram'
            );
            """
        )
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(memories_meta)").fetchall()
        }
        if "content_hash" not in columns:
            self.conn.execute(
                "ALTER TABLE memories_meta ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
            )
        if "vector_hash" not in columns:
            self.conn.execute(
                "ALTER TABLE memories_meta ADD COLUMN vector_hash TEXT NOT NULL DEFAULT ''"
            )
        # Backfill integrity metadata from the existing derived rows. A subsequent
        # deep check still compares these hashes with Markdown and a fresh embedding.
        missing = self.conn.execute(
            "SELECT id, scope, memory_type, confidence, last_verified, created_at"
            " FROM memories_meta WHERE content_hash = '' OR vector_hash = ''"
        ).fetchall()
        for row in missing:
            fts_row = self.conn.execute(
                "SELECT content FROM memories_fts WHERE id = ?", (row[0],)
            ).fetchone()
            vec_row = self.conn.execute(
                "SELECT embedding FROM memories_vec WHERE memory_id = ?", (row[0],)
            ).fetchone()
            if fts_row is None or vec_row is None:
                continue
            payload = {
                "id": row[0],
                "scope": row[1],
                "memory_type": row[2],
                "confidence": row[3],
                "last_verified": row[4],
                "created_at": row[5],
                "index_text": fts_row[0],
            }
            content_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            vector_hash = hashlib.sha256(bytes(vec_row[0])).hexdigest()
            self.conn.execute(
                "UPDATE memories_meta SET content_hash = ?, vector_hash = ? WHERE id = ?",
                (content_hash, vector_hash, row[0]),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def entry_hash(entry: MemoryEntry) -> str:
        payload = {
            "id": entry.id,
            "scope": entry.scope,
            "memory_type": entry.memory_type,
            "confidence": entry.confidence,
            "last_verified": entry.last_verified.isoformat(),
            "created_at": entry.created_at.isoformat(),
            "index_text": entry.index_text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def vector_hash(vector: list[float]) -> str:
        return hashlib.sha256(sqlite_vec.serialize_float32(vector)).hexdigest()

    def upsert(self, entry: MemoryEntry, vector: list[float]) -> None:
        """写入/更新一条记忆的 meta + 向量 + 全文索引。幂等：先删后插。"""
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(f"向量维度 {len(vector)} 不是 {EMBEDDING_DIM}（bge-m3）")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("向量包含 NaN 或 Inf，拒绝写入派生索引")
        if math.sqrt(sum(value * value for value in vector)) <= 1e-12:
            raise ValueError("向量范数为零或过小，拒绝写入派生索引")
        with self._lock:
            self.delete(entry.id, missing_ok=True)
            vector_blob = sqlite_vec.serialize_float32(vector)
            content_hash = self.entry_hash(entry)
            vector_hash = hashlib.sha256(vector_blob).hexdigest()
            self.conn.execute(
                "INSERT INTO memories_meta (id, scope, memory_type, confidence,"
                " last_verified, created_at, content_hash, vector_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.scope,
                    entry.memory_type,
                    entry.confidence,
                    entry.last_verified.isoformat(),
                    entry.created_at.isoformat(),
                    content_hash,
                    vector_hash,
                ),
            )
            self.conn.execute(
                "INSERT INTO memories_vec (embedding, memory_id) VALUES (?, ?)",
                (vector_blob, entry.id),
            )
            self.conn.execute(
                "INSERT INTO memories_fts (id, content) VALUES (?, ?)",
                # FTS 索引拼接文本（content + detail），提高召回；渲染仍用 content
                (entry.id, entry.index_text),
            )
            self.conn.commit()

    def delete(self, entry_id: str, *, missing_ok: bool = False) -> None:
        """从三张表删除。条目不存在时默认报错（fail-closed）。"""
        with self._lock:
            if not missing_ok and self.conn.execute(
                "SELECT 1 FROM memories_meta WHERE id = ?", (entry_id,)
            ).fetchone() is None:
                raise KeyError(f"索引中不存在 id {entry_id!r}")
            rowids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT rowid FROM memories_vec WHERE memory_id = ?", (entry_id,)
                )
            ]
            for rowid in rowids:
                self.conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid,))
            self.conn.execute("DELETE FROM memories_fts WHERE id = ?", (entry_id,))
            self.conn.execute("DELETE FROM memories_meta WHERE id = ?", (entry_id,))
            self.conn.commit()

    @staticmethod
    def _scope_clause(scopes: list[str] | None) -> tuple[str, list[str]]:
        """scope 过滤的 SQL 片段。scopes 为 None/空列表表示不过滤。"""
        if not scopes:
            return "", []
        placeholders = ", ".join("?" for _ in scopes)
        return f" AND m.scope IN ({placeholders})", list(scopes)

    def search_dense(
        self, query_vector: list[float], k: int, scopes: list[str] | None = None
    ) -> list[tuple[str, float]]:
        """向量近邻检索，返回 [(id, cosine_distance)] 按距离升序。

        vec0 的 KNN 查询不允许 WHERE 约束辅助列（join meta 也会被优化器下推而报错），
        所以分两步：先放大 k 取 KNN 候选，再用 SQL 在 memories_meta 上过滤 scope
        （传入当前 scope + global），保持候选顺序截断到 k。
        """
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM memories_meta").fetchone()[0]
            if total == 0:
                return []
            if scopes:
                placeholders = ", ".join("?" for _ in scopes)
                allowed = {
                    r[0]
                    for r in self.conn.execute(
                        f"SELECT id FROM memories_meta WHERE scope IN ({placeholders})",
                        list(scopes),
                    )
                }
                if not allowed:
                    return []
                fetch_k = min(total, max(k * 4, 50))
            else:
                allowed = None
                fetch_k = min(total, k)
            while True:
                rows = self.conn.execute(
                    "SELECT memory_id, distance FROM memories_vec"
                    " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (sqlite_vec.serialize_float32(query_vector), fetch_k),
                ).fetchall()
                filtered = [r for r in rows if allowed is None or r[0] in allowed]
                if len(filtered) >= k or fetch_k >= total:
                    rows = filtered
                    break
                fetch_k = min(total, fetch_k * 2)
        return [(r[0], float(r[1])) for r in rows[:k]]

    def search_sparse(
        self, query: str, k: int, scopes: list[str] | None = None
    ) -> list[tuple[str, float]]:
        """FTS5 全文检索（trigram，bm25），返回 [(id, bm25_score)] 按相关度降序。

        查询整体作为短语匹配（trigram 下等价于子串匹配），双引号转义防注入。
        注意：trigram 要求查询至少 3 个字符，更短的查询（如"端口"）不会命中——
        混合检索里这类短词由稠密向量路兜底。
        """
        escaped = query.replace('"', '""')
        scope_sql, params = self._scope_clause(scopes)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT memories_fts.id, bm25(memories_fts) AS score
                FROM memories_fts
                JOIN memories_meta m ON m.id = memories_fts.id
                WHERE memories_fts MATCH ?{scope_sql}
                ORDER BY score
                LIMIT ?
                """,
                (f'"{escaped}"', *params, k),
            ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def rebuild_from_markdown(self, memory_dir: Path, embedder) -> int:
        """全量清空后从 Markdown 记忆层重建索引，返回索引条目数。

        embedder 需提供 embed_texts(list[str]) -> list[list[float]]。
        """
        memory_dir = Path(memory_dir)
        with self._lock:
            self.conn.executescript(
                "DELETE FROM memories_vec; DELETE FROM memories_fts; DELETE FROM memories_meta;"
            )
            files = sorted(memory_dir.glob("*/*.md")) if memory_dir.exists() else []
            entries = [
                entry_from_markdown(f.read_text(encoding="utf-8"), path=f) for f in files
            ]
            if entries:
                vectors = embedder.embed_texts([e.index_text for e in entries])
                for entry, vector in zip(entries, vectors, strict=True):
                    self.upsert(entry, vector)
            self.conn.commit()
        return len(entries)

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM memories_meta").fetchone()[0]

    def list_ids(self) -> list[str]:
        """列出派生索引任一表中的全部 id，包含孤立派生行。"""
        with self._lock:
            return [
                r[0]
                for r in self.conn.execute(
                    "SELECT id FROM memories_meta UNION SELECT memory_id FROM memories_vec "
                    "UNION SELECT id FROM memories_fts ORDER BY id"
                ).fetchall()
            ]

    def integrity_records(self) -> dict[str, dict[str, object]]:
        """Return stored and self-observed hashes for deep consistency checks."""
        with self._lock:
            meta = {
                row[0]: {
                    "scope": row[1],
                    "memory_type": row[2],
                    "confidence": row[3],
                    "last_verified": row[4],
                    "created_at": row[5],
                    "content_hash": row[6],
                    "vector_hash": row[7],
                }
                for row in self.conn.execute(
                    "SELECT id, scope, memory_type, confidence, last_verified, created_at,"
                    " content_hash, vector_hash FROM memories_meta"
                ).fetchall()
            }
            fts_rows = self.conn.execute(
                "SELECT id, content FROM memories_fts"
            ).fetchall()
            vector_rows = self.conn.execute(
                "SELECT memory_id, embedding FROM memories_vec"
            ).fetchall()
            fts: dict[str, str] = {}
            fts_counts: dict[str, int] = {}
            for entry_id, content in fts_rows:
                fts_counts[entry_id] = fts_counts.get(entry_id, 0) + 1
                fts[entry_id] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            vectors: dict[str, str] = {}
            vector_values: dict[str, tuple[float, ...]] = {}
            vector_counts: dict[str, int] = {}
            for entry_id, raw_embedding in vector_rows:
                blob = bytes(raw_embedding)
                vector_counts[entry_id] = vector_counts.get(entry_id, 0) + 1
                vectors[entry_id] = hashlib.sha256(blob).hexdigest()
                if len(blob) % 4 == 0:
                    vector_values[entry_id] = struct.unpack(
                        f"<{len(blob) // 4}f", blob
                    )
        return {
            entry_id: {
                **hashes,
                "fts_hash": fts.get(entry_id, ""),
                "fts_count": fts_counts.get(entry_id, 0),
                "stored_vector_hash": vectors.get(entry_id, ""),
                "vector_count": vector_counts.get(entry_id, 0),
                "vector": vector_values.get(entry_id, ()),
            }
            for entry_id, hashes in meta.items()
        }

    def get_meta(self, entry_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT id, scope, memory_type, confidence, last_verified, created_at"
                " FROM memories_meta WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "scope": row[1],
            "memory_type": row[2],
            "confidence": row[3],
            "last_verified": date.fromisoformat(row[4]),
            "created_at": date.fromisoformat(row[5]),
        }

    def debug_dump(self) -> str:
        """调试用途：meta 表的 JSON 快照。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, scope, confidence FROM memories_meta ORDER BY id"
            ).fetchall()
        return json.dumps(rows, ensure_ascii=False)
