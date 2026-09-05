"""Markdown 正式层与 SQLite 派生索引的协调写入。

SQLite 与文件系统无法组成真正的 ACID 事务。本模块采用：完整校验、预计算向量、
进程锁、失败补偿和显式一致性检查。任何一步失败都尽力恢复操作前的 Markdown
事实，并用预计算的旧向量恢复索引；补偿本身失败时抛出包含两段错误的异常，绝不
把部分成功伪装成成功。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_memory.io_utils import interprocess_lock
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import MemoryEntry, validate_entry_id


class CoordinatedWriteError(RuntimeError):
    """协调写入失败；消息说明原始失败及补偿是否成功。"""


@dataclass(frozen=True)
class ConsistencyReport:
    markdown_only: tuple[str, ...]
    index_only: tuple[str, ...]

    @property
    def consistent(self) -> bool:
        return not self.markdown_only and not self.index_only


class MemoryWriter:
    def __init__(self, store: MarkdownStore, index: IndexDB, embedder):
        self.store = store
        self.index = index
        self.embedder = embedder
        self.lock_path = store.data_dir / "state" / "memory_write.lock"

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

    def check_consistency(self) -> ConsistencyReport:
        memory_ids = {e.id for e in self.store.list()}
        index_ids = set(self.index.list_ids())
        return ConsistencyReport(
            tuple(sorted(memory_ids - index_ids)), tuple(sorted(index_ids - memory_ids))
        )

    def create(self, entry: MemoryEntry) -> MemoryEntry:
        entry = self._validated(entry)
        vector = self._vector(entry)
        with interprocess_lock(self.lock_path):
            self.store.create(entry)
            try:
                self.index.upsert(entry, vector)
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
                raise CoordinatedWriteError(
                    f"索引写入失败，Markdown 已回滚：{original}"
                ) from original
        return entry

    def update(self, entry: MemoryEntry) -> MemoryEntry:
        entry = self._validated(entry)
        validate_entry_id(entry.id)
        with interprocess_lock(self.lock_path):
            old = self.store.get(entry.id)
            old_vector = self._vector(old)
            new_vector = self._vector(entry)
            updated = self.store.update(entry)
            try:
                self.index.upsert(updated, new_vector)
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
                raise CoordinatedWriteError(f"索引更新失败，原条目已恢复：{original}") from original
        return updated

    def delete(self, entry_id: str) -> MemoryEntry:
        validate_entry_id(entry_id)
        with interprocess_lock(self.lock_path):
            old = self.store.get(entry_id)
            old_vector = self._vector(old)
            self.store.delete(entry_id)
            try:
                self.index.delete(entry_id)
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
                raise CoordinatedWriteError(f"索引删除失败，原条目已恢复：{original}") from original
        return old

    def replace(self, old_id: str, new_entry: MemoryEntry) -> MemoryEntry:
        """用 new_entry 取代 old_id；新条目完整落地后才删除旧条目。"""

        validate_entry_id(old_id)
        new_entry = self._validated(new_entry)
        if old_id == new_entry.id:
            return self.update(new_entry)
        with interprocess_lock(self.lock_path):
            old = self.store.get(old_id)
            # 显式预检，避免删旧后才发现新 id 冲突。
            try:
                self.store.get(new_entry.id)
            except KeyError:
                pass
            else:
                raise ValueError(f"替代条目的 id {new_entry.id!r} 已存在，拒绝删除旧条目")
            old_vector = self._vector(old)
            new_vector = self._vector(new_entry)
            self.store.create(new_entry)
            try:
                self.index.upsert(new_entry, new_vector)
                self.store.delete(old_id)
                self.index.delete(old_id)
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
                raise CoordinatedWriteError(f"替代写入失败，旧条目已恢复：{original}") from original
        return new_entry
