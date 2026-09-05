"""Markdown 正式层与 SQLite 派生索引的协调写入。

SQLite 与文件系统无法组成真正的 ACID 事务。本模块采用：完整校验、预计算向量、
进程锁、失败补偿和显式一致性检查。任何一步失败都尽力恢复操作前的 Markdown
事实，并用预计算的旧向量恢复索引；补偿本身失败时抛出包含两段错误的异常，绝不
把部分成功伪装成成功。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from agent_memory.io_utils import atomic_write_text, interprocess_lock
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore, MemoryStoreError
from agent_memory.models import MemoryEntry, validate_entry_id


class CoordinatedWriteError(RuntimeError):
    """协调写入失败；消息说明原始失败及补偿是否成功。"""


@dataclass(frozen=True)
class ConsistencyReport:
    markdown_only: tuple[str, ...]
    index_only: tuple[str, ...]
    mismatched: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.markdown_only and not self.index_only and not self.mismatched


class MemoryWriter:
    def __init__(
        self, store: MarkdownStore, index: IndexDB, embedder, *, recover: bool = True
    ):
        self.store = store
        self.index = index
        self.embedder = embedder
        self.lock_path = store.data_dir / "state" / "memory_write.lock"
        self.journal_path = store.data_dir / "state" / "memory_write_journal.json"
        self.evolution_journal_path = (
            store.data_dir / "state" / "evolution_apply_journal.json"
        )
        self.recovery_log = store.data_dir / "logs" / "memory_recovery.jsonl"
        self.recover_before_write = recover
        if recover:
            self.recover_pending()

    @staticmethod
    def _validated(entry: MemoryEntry) -> MemoryEntry:
        return MemoryEntry.model_validate(entry.model_dump(mode="python"))

    def _vector(self, entry: MemoryEntry) -> list[float]:
        return self.embedder.embed_texts([entry.index_text])[0]

    @staticmethod
    def _compensate(steps) -> None:
        """Attempt every rollback step and report all failures together."""
        errors: list[str] = []
        for label, step in steps:
            try:
                step()
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _delete_store_if_present(self, entry_id: str) -> None:
        try:
            self.store.delete(entry_id)
        except KeyError:
            pass

    def _begin(
        self,
        operation: str,
        *,
        before: list[MemoryEntry] | None = None,
        after: list[MemoryEntry] | None = None,
    ) -> dict:
        if self.recover_before_write and self.evolution_journal_path.exists():
            raise CoordinatedWriteError(
                f"存在未恢复的进化事务：{self.evolution_journal_path}，拒绝开始新写入"
            )
        if self.journal_path.exists():
            raise CoordinatedWriteError(
                f"存在未恢复的写入日志：{self.journal_path}，拒绝开始新写入"
            )
        record = {
            "version": 2,
            "transaction_id": uuid.uuid4().hex,
            "operation": operation,
            "phase": "prepared",
            "before": [entry.model_dump(mode="json") for entry in before or []],
            "after": [entry.model_dump(mode="json") for entry in after or []],
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_write_text(
            self.journal_path,
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )
        return record

    def _mark_committed(self, record: dict, after: list[MemoryEntry]) -> None:
        """Persist the transaction commit point before the journal is removed."""
        committed = {
            **record,
            "phase": "committed",
            "after": [entry.model_dump(mode="json") for entry in after],
            "committed_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_write_text(
            self.journal_path,
            json.dumps(committed, ensure_ascii=False, sort_keys=True),
        )

    def _clear_journal(self) -> None:
        self.journal_path.unlink(missing_ok=True)

    def _append_recovery(self, event: str, **details) -> None:
        self.recovery_log.parent.mkdir(parents=True, exist_ok=True)
        recovery = {
            "event": event,
            "recovered_at": datetime.now().isoformat(timespec="seconds"),
            **details,
        }
        with self.recovery_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(recovery, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _recover_evolution(self) -> bool:
        if not self.evolution_journal_path.exists():
            return False
        try:
            record = json.loads(self.evolution_journal_path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("evolution journal must be an object")
            snapshot_id = record.get("snapshot_id")
            if record.get("version") != 1 or not isinstance(snapshot_id, str):
                raise ValueError("evolution journal schema invalid")
            snapshot_path = self.store.data_dir / "snapshots" / snapshot_id / "memory"
            expected_parent = (self.store.data_dir / "snapshots").resolve()
            if snapshot_path.resolve().parent.parent != expected_parent:
                raise ValueError("evolution snapshot path escapes snapshots directory")
            if not snapshot_path.is_dir():
                raise FileNotFoundError(f"evolution snapshot missing: {snapshot_path}")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CoordinatedWriteError(
                f"进化恢复日志损坏，拒绝自动恢复：{self.evolution_journal_path}: {exc}"
            ) from exc
        try:
            if self.store.memory_dir.exists():
                shutil.rmtree(self.store.memory_dir)
            shutil.copytree(snapshot_path, self.store.memory_dir)
            rebuilt = self.index.rebuild_from_markdown(
                self.store.memory_dir, self.embedder
            )
            report = self.check_consistency()
            if not report.consistent:
                raise CoordinatedWriteError(
                    f"进化恢复后仍不一致：markdown_only={report.markdown_only}, "
                    f"index_only={report.index_only}, mismatched={report.mismatched}"
                )
        except CoordinatedWriteError:
            raise
        except Exception as exc:
            raise CoordinatedWriteError(
                "进化事务自动恢复失败，恢复日志已保留且新写入将被拒绝："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._append_recovery(
            "evolution_apply_recovered", rebuilt_entries=rebuilt, journal=record
        )
        self.journal_path.unlink(missing_ok=True)
        self.evolution_journal_path.unlink()
        return True

    def recover_pending(self) -> bool:
        """Replay an interrupted write by rebuilding the derived index from Markdown."""
        with interprocess_lock(self.lock_path):
            if self._recover_evolution():
                return True
            if not self.journal_path.exists():
                return False
            try:
                record = json.loads(self.journal_path.read_text(encoding="utf-8"))
                version = record.get("version") if isinstance(record, dict) else None
                if (
                    not isinstance(record, dict)
                    or version not in {1, 2}
                    or not isinstance(record.get("transaction_id"), str)
                    or not isinstance(record.get("operation"), str)
                    or not isinstance(record.get("before"), list)
                    or (
                        version == 1
                        and not isinstance(record.get("created_ids"), list)
                    )
                    or (
                        version == 2
                        and (
                            record.get("phase") not in {"prepared", "committed"}
                            or not isinstance(record.get("after"), list)
                        )
                    )
                ):
                    raise ValueError("journal schema invalid")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise CoordinatedWriteError(
                    f"写入恢复日志损坏，拒绝自动恢复：{self.journal_path}: {exc}"
                ) from exc
            try:
                before = [MemoryEntry.model_validate(item) for item in record["before"]]
                if record["version"] == 1:
                    after_ids = [
                        validate_entry_id(item) for item in record["created_ids"]
                    ]
                    after: list[MemoryEntry] = []
                    phase = "prepared"
                else:
                    after = [
                        MemoryEntry.model_validate(item) for item in record["after"]
                    ]
                    after_ids = [entry.id for entry in after]
                    phase = record["phase"]
                operation = record["operation"]
                if operation not in {"create", "update", "delete", "replace"}:
                    raise ValueError(f"unknown operation {operation!r}")
                if record["version"] == 2:
                    shape = (len(before), len(after))
                    valid_shape = {
                        "create": (0, 1),
                        "update": (1, 1),
                        "delete": (1, 0),
                        "replace": (1, 1),
                    }[operation]
                    if shape != valid_shape:
                        raise ValueError(
                            f"{operation} journal intent has shape {shape}, "
                            f"expected {valid_shape}"
                        )
                    if operation == "update" and before[0].id != after[0].id:
                        raise ValueError("update journal changes id")
                    if operation == "replace" and before[0].id == after[0].id:
                        raise ValueError("replace journal keeps the same id")
            except (TypeError, ValueError) as exc:
                raise CoordinatedWriteError(
                    f"写入恢复日志内容非法，拒绝自动恢复：{self.journal_path}: {exc}"
                ) from exc
            if phase == "prepared":
                # No durable commit point: restore the exact pre-transaction image.
                for entry_id in after_ids:
                    if entry_id not in {entry.id for entry in before}:
                        self._delete_store_if_present(entry_id)
                for entry in before:
                    self.store.restore(entry)
            else:
                # Commit marker is durable: fail closed if the Markdown fact layer
                # does not contain exactly the committed transaction result.
                before_ids = {entry.id for entry in before}
                after_by_id = {entry.id: entry for entry in after}
                for removed_id in before_ids - set(after_by_id):
                    try:
                        self.store.get(removed_id)
                    except KeyError:
                        pass
                    else:
                        raise CoordinatedWriteError(
                            f"已提交事务的旧条目 {removed_id!r} 仍存在，拒绝猜测恢复"
                        )
                for entry_id, expected in after_by_id.items():
                    try:
                        current = self.store.get(entry_id)
                    except KeyError as exc:
                        raise CoordinatedWriteError(
                            f"已提交事务缺少结果条目 {entry_id!r}，拒绝猜测恢复"
                        ) from exc
                    if current != expected:
                        raise CoordinatedWriteError(
                            f"已提交事务结果条目 {entry_id!r} 与日志不符，拒绝猜测恢复"
                        )
            rebuilt = self.index.rebuild_from_markdown(self.store.memory_dir, self.embedder)
            report = self.check_consistency()
            if not report.consistent:
                raise CoordinatedWriteError(
                    f"写入恢复后仍不一致：markdown_only={report.markdown_only}, "
                    f"index_only={report.index_only}, mismatched={report.mismatched}"
                )
            self._append_recovery(
                "memory_write_recovered", rebuilt_entries=rebuilt, journal=record
            )
            self._clear_journal()
            return True

    def _recover_before_new_write(self) -> None:
        """Resolve every older transaction before accepting a new write.

        This is required even for a long-lived writer: an evolution apply can fail
        after this instance was constructed and leave a durable recovery marker.
        Inner writers owned by apply_proposal opt out because the outer evolution
        transaction deliberately owns that marker and the shared write lock.
        """
        if self.recover_before_write and (
            self.evolution_journal_path.exists() or self.journal_path.exists()
        ):
            try:
                self.recover_pending()
            except CoordinatedWriteError:
                raise
            except Exception as exc:
                raise CoordinatedWriteError(
                    "存在未恢复事务且自动恢复失败，拒绝接受新写入："
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    @contextmanager
    def write_guard(self) -> Iterator[None]:
        """Hold the global write lock and recover before any read-decide-write flow."""
        with interprocess_lock(self.lock_path):
            self._recover_before_new_write()
            yield

    def check_consistency(self) -> ConsistencyReport:
        entries = {entry.id: entry for entry in self.store.list()}
        memory_ids = set(entries)
        index_ids = set(self.index.list_ids())
        shared = sorted(memory_ids & index_ids)
        records = self.index.integrity_records()
        fresh_vectors = (
            self.embedder.embed_texts([entries[entry_id].index_text for entry_id in shared])
            if shared
            else []
        )
        mismatched: list[str] = []
        for entry_id, fresh_vector in zip(shared, fresh_vectors, strict=True):
            entry = entries[entry_id]
            record = records.get(entry_id, {})
            stored_vector = record.get("vector", ())
            vector_matches = self._vectors_equivalent(stored_vector, fresh_vector)
            if (
                record.get("scope") != entry.scope
                or record.get("memory_type") != entry.memory_type
                or record.get("confidence") != entry.confidence
                or record.get("last_verified") != entry.last_verified.isoformat()
                or record.get("created_at") != entry.created_at.isoformat()
                or record.get("content_hash") != self.index.entry_hash(entry)
                or record.get("fts_hash")
                != hashlib.sha256(entry.index_text.encode("utf-8")).hexdigest()
                or record.get("stored_vector_hash") != record.get("vector_hash")
                or record.get("fts_count") != 1
                or record.get("vector_count") != 1
                or not vector_matches
            ):
                mismatched.append(entry_id)
        return ConsistencyReport(
            tuple(sorted(memory_ids - index_ids)),
            tuple(sorted(index_ids - memory_ids)),
            tuple(mismatched),
        )

    @staticmethod
    def _vectors_equivalent(stored, fresh) -> bool:
        """Compare embeddings numerically, tolerating legitimate batch rounding."""
        if len(stored) != len(fresh) or len(stored) != 1024:
            return False
        if not all(math.isfinite(value) for value in (*stored, *fresh)):
            return False
        dot = sum(left * right for left, right in zip(stored, fresh, strict=True))
        stored_norm = math.sqrt(sum(value * value for value in stored))
        fresh_norm = math.sqrt(sum(value * value for value in fresh))
        if stored_norm <= 1e-12 or fresh_norm <= 1e-12:
            return False
        # sqlite-vec's cosine implementation is not scale-invariant for extreme
        # float32 magnitudes. Embedders in this project promise L2-normalized output,
        # so a persisted norm must remain numerically close to a fresh one.
        if not math.isclose(stored_norm, fresh_norm, rel_tol=1e-4, abs_tol=1e-6):
            return False
        return dot / (stored_norm * fresh_norm) >= 0.99999

    def create(self, entry: MemoryEntry) -> MemoryEntry:
        entry = self._validated(entry)
        vector = self._vector(entry)
        with interprocess_lock(self.lock_path):
            self._recover_before_new_write()
            try:
                self.store.get(entry.id)
            except KeyError:
                pass
            else:
                raise MemoryStoreError(f"id {entry.id!r} 已存在，拒绝覆盖")
            journal = self._begin("create", after=[entry])
            try:
                self.store.create(entry)
                self.index.upsert(entry, vector)
                self._mark_committed(journal, [entry])
            except Exception as original:
                try:
                    self._compensate([
                        ("remove Markdown", lambda: self._delete_store_if_present(entry.id)),
                        ("remove index", lambda: self.index.delete(entry.id, missing_ok=True)),
                    ])
                except Exception as rollback:
                    raise CoordinatedWriteError(
                        f"索引写入失败且 Markdown 回滚失败：{original}; rollback={rollback}"
                    ) from original
                self._clear_journal()
                raise CoordinatedWriteError(
                    f"索引写入失败，Markdown 已回滚：{original}"
                ) from original
            self._clear_journal()
        return entry

    @staticmethod
    def _assert_expected(current: MemoryEntry, expected: MemoryEntry | None) -> None:
        if expected is None:
            return
        fields = ("id", "version", "index_text", "confidence", "scope", "last_verified")
        if any(getattr(current, field) != getattr(expected, field) for field in fields):
            raise CoordinatedWriteError(
                f"条目 {current.id!r} 已在读取后发生变化，拒绝覆盖并发更新"
            )

    def update(
        self, entry: MemoryEntry, *, expected: MemoryEntry | None = None
    ) -> MemoryEntry:
        entry = self._validated(entry)
        validate_entry_id(entry.id)
        with interprocess_lock(self.lock_path):
            self._recover_before_new_write()
            old = self.store.get(entry.id)
            self._assert_expected(old, expected)
            # Retrieval tracking updates only this counter and does not advance the
            # semantic version. Preserve the lock-current value during any content
            # or confidence update so a recent retrieval cannot be overwritten.
            entry = MemoryEntry.model_validate(
                entry.model_dump(mode="python")
                | {"retrieval_count": old.retrieval_count}
            )
            old_vector = self._vector(old)
            new_vector = self._vector(entry)
            journal = self._begin("update", before=[old], after=[entry])
            try:
                updated = self.store.update(entry)
                self.index.upsert(updated, new_vector)
                self._mark_committed(journal, [updated])
            except Exception as original:
                try:
                    self._compensate([
                        ("restore Markdown", lambda: self.store.restore(old)),
                        ("restore index", lambda: self.index.upsert(old, old_vector)),
                    ])
                except Exception as rollback:
                    raise CoordinatedWriteError(
                        f"索引更新失败且补偿失败：{original}; rollback={rollback}"
                    ) from original
                self._clear_journal()
                raise CoordinatedWriteError(f"索引更新失败，原条目已恢复：{original}") from original
            self._clear_journal()
        return updated

    def mutate(self, entry_id: str, transform) -> MemoryEntry:
        """Read and transform an entry under the same lock used for the coordinated update."""
        validate_entry_id(entry_id)
        with interprocess_lock(self.lock_path):
            self._recover_before_new_write()
            current = self.store.get(entry_id)
            candidate = self._validated(transform(current))
            if candidate.id != entry_id:
                raise ValueError("mutate 不允许变更 entry id")
            return self.update(candidate)

    def delete(
        self, entry_id: str, *, expected: MemoryEntry | None = None
    ) -> MemoryEntry:
        validate_entry_id(entry_id)
        with interprocess_lock(self.lock_path):
            self._recover_before_new_write()
            old = self.store.get(entry_id)
            self._assert_expected(old, expected)
            old_vector = self._vector(old)
            journal = self._begin("delete", before=[old])
            try:
                self.store.delete(entry_id)
                self.index.delete(entry_id)
                self._mark_committed(journal, [])
            except Exception as original:
                try:
                    self._compensate([
                        ("restore Markdown", lambda: self.store.restore(old)),
                        ("restore index", lambda: self.index.upsert(old, old_vector)),
                    ])
                except Exception as rollback:
                    raise CoordinatedWriteError(
                        f"索引删除失败且补偿失败：{original}; rollback={rollback}"
                    ) from original
                self._clear_journal()
                raise CoordinatedWriteError(f"索引删除失败，原条目已恢复：{original}") from original
            self._clear_journal()
        return old

    def replace(
        self,
        old_id: str,
        new_entry: MemoryEntry,
        *,
        expected: MemoryEntry | None = None,
    ) -> MemoryEntry:
        """用 new_entry 取代 old_id；新条目完整落地后才删除旧条目。"""

        validate_entry_id(old_id)
        new_entry = self._validated(new_entry)
        if old_id == new_entry.id:
            return self.update(new_entry, expected=expected)
        with interprocess_lock(self.lock_path):
            self._recover_before_new_write()
            old = self.store.get(old_id)
            self._assert_expected(old, expected)
            # 显式预检，避免删旧后才发现新 id 冲突。
            try:
                self.store.get(new_entry.id)
            except KeyError:
                pass
            else:
                raise MemoryStoreError(
                    f"替代条目的 id {new_entry.id!r} 已存在，拒绝删除旧条目"
                )
            old_vector = self._vector(old)
            new_vector = self._vector(new_entry)
            journal = self._begin("replace", before=[old], after=[new_entry])
            try:
                self.store.create(new_entry)
                self.index.upsert(new_entry, new_vector)
                self.store.delete(old_id)
                self.index.delete(old_id)
                self._mark_committed(journal, [new_entry])
            except Exception as original:
                try:
                    # 恢复旧事实，移除可能落下一半的新事实。
                    self._compensate([
                        ("restore old Markdown", lambda: self.store.restore(old)),
                        ("restore old index", lambda: self.index.upsert(old, old_vector)),
                        (
                            "remove new Markdown",
                            lambda: self._delete_store_if_present(new_entry.id),
                        ),
                        (
                            "remove new index",
                            lambda: self.index.delete(new_entry.id, missing_ok=True),
                        ),
                    ])
                except Exception as rollback:
                    raise CoordinatedWriteError(
                        f"替代写入失败且补偿失败：{original}; rollback={rollback}"
                    ) from original
                self._clear_journal()
                raise CoordinatedWriteError(f"替代写入失败，旧条目已恢复：{original}") from original
            self._clear_journal()
        return new_entry
