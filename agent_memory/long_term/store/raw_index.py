"""原文归档的派生检索索引（P03，v0.2）：data/raw_index.db。

与 index.db 一样是**派生物**（红线 D1）：
随时可以从 data/raw 全量重建（rebuild_from_raw），绝不手改。
每条归档记录（data/raw/<source>/<session_id>.jsonl 的一行）一行索引：
- raw_meta：source、session_id、line（1 起，与 EvidenceRef.line_range 同口径）、
  role、date、scope、host；
- raw_vec：vec0 虚表（bge-m3 1024 维，cosine）；
- raw_fts：FTS5 trigram 全文。
检索 = 稠密 + 稀疏两路 → RRF 融合（与记忆层检索同一套公式），scope 过滤在 SQL 层完成。

会话级元数据（日期、作用域、宿主）存在 data/raw/<source>/<session_id>.meta.json，
归档时写一次；它只是新增文件，不改写任何已归档的原文行。
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

EMBEDDING_DIM = 1024
RRF_K = 60
CANDIDATES = 30


@dataclass
class RawHit:
    source: str
    session_id: str
    line: int
    role: str
    content: str
    date: str | None
    scope: str | None
    score: float


class RawIndex:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS raw_meta (
                rid INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                line INTEGER NOT NULL,
                role TEXT NOT NULL,
                date TEXT,
                scope TEXT,
                host TEXT,
                UNIQUE (source, session_id, line)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS raw_vec USING vec0 (
                embedding float[{EMBEDDING_DIM}] distance_metric=cosine,
                +rid INTEGER
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5 (
                rid UNINDEXED,
                content,
                tokenize='trigram'
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- 写

    def add(
        self,
        source: str,
        session_id: str,
        start_line: int,
        records: list[dict],
        embedder,
        *,
        date: str | None = None,
        scope: str | None = None,
        host: str | None = None,
    ) -> int:
        """把一段连续归档记录加入索引（line 从 start_line 起）。已存在的行跳过（幂等）。"""
        rows = []
        for i, rec in enumerate(records):
            content = str(rec.get("content") or "").strip()
            role = str(rec.get("role") or "")
            if not content or role not in {"user", "assistant", "tool"}:
                continue
            rows.append((start_line + i, role, content))
        if not rows:
            return 0
        vectors = embedder.embed_texts([c for _, _, c in rows])
        added = 0
        with self._lock:
            for (line, role, content), vec in zip(rows, vectors, strict=True):
                if len(vec) != EMBEDDING_DIM or not all(math.isfinite(v) for v in vec):
                    continue
                try:
                    cur = self.conn.execute(
                        "INSERT INTO raw_meta (source, session_id, line, role, date, scope, host)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (source, session_id, line, role, date, scope, host),
                    )
                except sqlite3.IntegrityError:
                    continue
                rid = cur.lastrowid
                self.conn.execute(
                    "INSERT INTO raw_vec (embedding, rid) VALUES (?, ?)",
                    (sqlite_vec.serialize_float32(vec), rid),
                )
                self.conn.execute(
                    "INSERT INTO raw_fts (rid, content) VALUES (?, ?)", (rid, content)
                )
                added += 1
            self.conn.commit()
        return added

    def replace_content(
        self, source: str, session_id: str, line: int, content: str, embedder
    ) -> bool:
        """遗忘请求擦除原文片段后，同步更新该行的索引（向量与全文）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT rid FROM raw_meta WHERE source = ? AND session_id = ? AND line = ?",
                (source, session_id, line),
            ).fetchone()
            if row is None:
                return False
            rid = row[0]
            vec = embedder.embed_texts([content])[0]
            for (vrow,) in self.conn.execute(
                "SELECT rowid FROM raw_vec WHERE rid = ?", (rid,)
            ).fetchall():
                self.conn.execute("DELETE FROM raw_vec WHERE rowid = ?", (vrow,))
            self.conn.execute(
                "INSERT INTO raw_vec (embedding, rid) VALUES (?, ?)",
                (sqlite_vec.serialize_float32(vec), rid),
            )
            self.conn.execute("DELETE FROM raw_fts WHERE rid = ?", (rid,))
            self.conn.execute("INSERT INTO raw_fts (rid, content) VALUES (?, ?)", (rid, content))
            self.conn.commit()
        return True

    # ---------------------------------------------------------------- 读

    def _meta(self, rid: int) -> tuple | None:
        return self.conn.execute(
            "SELECT m.source, m.session_id, m.line, m.role, f.content, m.date, m.scope"
            " FROM raw_meta m JOIN raw_fts f ON f.rid = m.rid WHERE m.rid = ?",
            (rid,),
        ).fetchone()

    def search(
        self,
        query: str,
        embedder,
        k: int = 5,
        scopes: list[str] | None = None,
        session_id: str | None = None,
    ) -> list[RawHit]:
        """稠密 + 稀疏两路 → RRF。

        scopes 为 None 表示不过滤；否则 scope 为空（未标注）的记录也可见。
        """
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM raw_meta").fetchone()[0]
            if total == 0:
                return []

            def allowed(rid: int) -> bool:
                row = self.conn.execute(
                    "SELECT scope, session_id FROM raw_meta WHERE rid = ?", (rid,)
                ).fetchone()
                if row is None:
                    return False
                if session_id is not None and row[1] != session_id:
                    return False
                return scopes is None or row[0] is None or row[0] in scopes

            qv = embedder.embed_texts([query])[0]
            fetch = min(total, max(CANDIDATES * 3, k * 6))
            dense = [
                r[0]
                for r in self.conn.execute(
                    "SELECT rid, distance FROM raw_vec WHERE embedding MATCH ? AND k = ? ORDER BY"
                    " distance",
                    (sqlite_vec.serialize_float32(qv), fetch),
                ).fetchall()
                if allowed(r[0])
            ][:CANDIDATES]
            sparse: list[int] = []
            q = query.strip()
            if len(q) >= 3:
                escaped = q.replace('"', '""')
                terms = [t for t in _terms(q) if len(t) >= 3]
                exprs = [f'"{escaped}"'] + [f'"{t.replace(chr(34), "")}"' for t in terms[:12]]
                seen: set[int] = set()
                for expr in exprs:
                    try:
                        rows = self.conn.execute(
                            "SELECT rid FROM raw_fts WHERE raw_fts MATCH ? ORDER BY bm25(raw_fts)"
                            " LIMIT ?",
                            (expr, CANDIDATES),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    for (rid,) in rows:
                        if rid not in seen and allowed(rid):
                            seen.add(rid)
                            sparse.append(rid)
            fused: dict[int, float] = {}
            for rank, rid in enumerate(dense, start=1):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
            for rank, rid in enumerate(sparse[:CANDIDATES], start=1):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
            hits = []
            for rid, score in sorted(fused.items(), key=lambda x: -x[1])[:k]:
                m = self._meta(rid)
                if m:
                    hits.append(RawHit(m[0], m[1], m[2], m[3], m[4], m[5], m[6], score))
        return hits

    def session_records(self, source: str, session_id: str) -> list[RawHit]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT m.source, m.session_id, m.line, m.role, f.content, m.date, m.scope FROM r"
                "aw_meta m"
                " JOIN raw_fts f ON f.rid = m.rid WHERE m.source = ? AND m.session_id = ? ORDER B"
                "Y m.line",
                (source, session_id),
            ).fetchall()
        return [RawHit(r[0], r[1], r[2], r[3], r[4], r[5], r[6], 0.0) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM raw_meta").fetchone()[0]

    # ---------------------------------------------------------------- 重建

    def rebuild_from_raw(self, raw_dir: Path, embedder) -> int:
        """清空后从 data/raw 全量重建（派生索引可重建，D1）。"""
        with self._lock:
            self.conn.executescript(
                "DELETE FROM raw_vec; DELETE FROM raw_fts; DELETE FROM raw_meta;"
            )
            self.conn.commit()
        n = 0
        for f in sorted(Path(raw_dir).glob("*/*.jsonl")):
            meta = read_session_meta(f)
            recs = []
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    recs.append({})
            n += self.add(
                f.parent.name,
                f.stem,
                1,
                recs,
                embedder,
                date=meta.get("date"),
                scope=meta.get("scope"),
                host=meta.get("host"),
            )
        return n


def _terms(text: str) -> list[str]:
    """查询拆词：按空白与中文标点切开，保留 ≥3 字符的片段用于 trigram 全文匹配。"""
    import re

    return [t for t in re.split(r"[\s，。、；：？！,.;:?!（）()“”\"'【】\[\]]+", text) if t]


def session_meta_path(archive_file: Path) -> Path:
    return archive_file.with_suffix(".meta.json")


def read_session_meta(archive_file: Path) -> dict:
    p = session_meta_path(archive_file)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
