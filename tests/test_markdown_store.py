"""MarkdownStore：往返一致性、scope 目录名映射、CRUD 语义。"""

from datetime import date

import pytest

from agent_memory.models import EvidenceRef
from agent_memory.store.markdown_store import (
    MemoryNotFoundError,
    MemoryStoreError,
    dirname_to_scope,
    scope_to_dirname,
)


class TestScopeDirnameMapping:
    """scope 里的冒号在 Windows 目录名非法，写入时替换成 "__"，读取时还原。"""

    @pytest.mark.parametrize(
        "scope, dirname",
        [
            ("global", "global"),
            ("repo:agent-memory", "repo__agent-memory"),
            ("agent:kimi-code", "agent__kimi-code"),
        ],
    )
    def test_round_trip(self, scope, dirname):
        assert scope_to_dirname(scope) == dirname
        assert dirname_to_scope(dirname) == scope

    def test_file_lands_in_mapped_dir(self, store, entry_factory, tmp_path):
        entry = entry_factory(entry_id="repo-scoped", scope="repo:agent-memory")
        store.create(entry)
        assert (tmp_path / "memory" / "repo__agent-memory" / "repo-scoped.md").exists()
        # 读回来 scope 已还原
        assert store.get("repo-scoped").scope == "repo:agent-memory"


class TestRoundTrip:
    def test_create_get_round_trip(self, store, entry_factory):
        entry = entry_factory(
            entry_id="with-evidence",
            content="用户偏好用 uv 管理 Python 环境。",
            memory_type="profile",
            scope="agent:kimi",
        )
        entry = entry.model_copy(
            update={"evidence": [EvidenceRef(session_id="s1", source="cli", line_range=(3, 7))]}
        )
        store.create(entry)
        got = store.get("with-evidence")
        assert got == entry  # 全字段一致，含 evidence.line_range 的 tuple/list 转换

    def test_markdown_layout(self, store, entry_factory, tmp_path):
        entry = entry_factory(entry_id="layout-check", content="正文内容保持原样。")
        store.create(entry)
        text = (tmp_path / "memory" / "global" / "layout-check.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "id: layout-check" in text
        assert text.rstrip().endswith("正文内容保持原样。")

    def test_detail_round_trip(self, store, entry_factory):
        """detail 存进 frontmatter，读回一致；无 detail 的老文件读回为 None。"""
        entry = entry_factory(entry_id="with-detail")
        entry = entry.model_copy(
            update={"detail": "用户在 s1 会话定下：项目 dev server 固定 8765 端口，"
                              "因为 8000 已被另一个项目占用。"}
        )
        store.create(entry)
        got = store.get("with-detail")
        assert got == entry
        assert got.detail is not None and "8765" in got.detail

        store.create(entry_factory(entry_id="no-detail"))
        assert store.get("no-detail").detail is None


class TestCrudSemantics:
    def test_create_duplicate_id_fails_closed(self, store, entry_factory):
        store.create(entry_factory(entry_id="dup"))
        with pytest.raises(MemoryStoreError, match="已存在"):
            store.create(entry_factory(entry_id="dup"))

    def test_create_duplicate_id_across_scopes_fails(self, store, entry_factory):
        store.create(entry_factory(entry_id="dup2", scope="global"))
        with pytest.raises(MemoryStoreError, match="已存在"):
            store.create(entry_factory(entry_id="dup2", scope="repo:other"))

    def test_update_bumps_version_and_last_verified(self, store, entry_factory):
        old_date = date(2026, 1, 1)
        store.create(entry_factory(entry_id="upd", last_verified=old_date))
        updated = store.update(store.get("upd").model_copy(update={"confidence": "medium"}))
        assert updated.version == 2
        assert updated.last_verified == date.today()
        assert updated.confidence == "medium"
        # 再更新一次 version 继续 +1
        assert store.update(updated).version == 3

    def test_update_rejects_scope_change(self, store, entry_factory):
        store.create(entry_factory(entry_id="scoped", scope="global"))
        with pytest.raises(MemoryStoreError, match="scope"):
            store.update(store.get("scoped").model_copy(update={"scope": "repo:x"}))

    def test_update_missing_raises(self, store, entry_factory):
        with pytest.raises(MemoryNotFoundError):
            store.update(entry_factory(entry_id="ghost"))

    def test_delete(self, store, entry_factory):
        store.create(entry_factory(entry_id="bye"))
        store.delete("bye")
        with pytest.raises(MemoryNotFoundError):
            store.get("bye")

    def test_list_by_scope_and_all(self, store, entry_factory):
        store.create(entry_factory(entry_id="a", scope="global"))
        store.create(entry_factory(entry_id="b", scope="repo:proj"))
        store.create(entry_factory(entry_id="c", scope="repo:proj"))
        assert [e.id for e in store.list()] == ["a", "b", "c"]
        assert [e.id for e in store.list(scope="repo:proj")] == ["b", "c"]
        assert store.list(scope="agent:nobody") == []

    def test_iter_all(self, store, entry_factory):
        store.create(entry_factory(entry_id="x"))
        store.create(entry_factory(entry_id="y", scope="agent:a"))
        assert {e.id for e in store.iter_all()} == {"x", "y"}

    def test_increment_retrieval_count(self, store, entry_factory):
        """命中计数 +1，不动 version / last_verified（区别于 update 的版本语义）。"""
        old_date = date(2026, 1, 1)
        store.create(entry_factory(entry_id="hit", last_verified=old_date))
        updated = store.increment_retrieval_count("hit")
        assert updated.retrieval_count == 1
        assert updated.version == 1
        assert updated.last_verified == old_date
        # 持久化：重新读出来也是 1，再 +1 是 2
        assert store.get("hit").retrieval_count == 1
        assert store.increment_retrieval_count("hit").retrieval_count == 2

    def test_increment_retrieval_count_missing_raises(self, store):
        with pytest.raises(MemoryNotFoundError):
            store.increment_retrieval_count("ghost")


class TestFailClosed:
    def test_invalid_frontmatter_raises(self, store, tmp_path):
        bad = tmp_path / "memory" / "global" / "bad-entry.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("---\nid: BAD_ID_UPPER\nscope: global\n---\n正文\n", encoding="utf-8")
        with pytest.raises(MemoryStoreError, match="校验失败"):
            store.get("bad-entry")

    def test_missing_frontmatter_raises(self, store, tmp_path):
        bad = tmp_path / "memory" / "global" / "no-fm.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("没有 frontmatter 的正文\n", encoding="utf-8")
        with pytest.raises(MemoryStoreError, match="frontmatter"):
            store.get("no-fm")
