"""IndexDB：增删查、scope 过滤、中文 trigram 全文、rebuild 幂等。"""

import math

import pytest

from agent_memory.long_term.store.index_db import EMBEDDING_DIM, IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore


def _vec(dim_one: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[dim_one] = 1.0
    return vec


class TestUpsertDelete:
    def test_upsert_and_count(self, index, entry_factory):
        index.upsert(entry_factory(entry_id="m1"), _vec(0))
        index.upsert(entry_factory(entry_id="m2", scope="repo:p"), _vec(1))
        assert index.count() == 2

    def test_upsert_is_idempotent(self, index, entry_factory):
        entry = entry_factory(entry_id="m1")
        index.upsert(entry, _vec(0))
        index.upsert(entry.model_copy(update={"confidence": "low"}), _vec(2))
        assert index.count() == 1
        assert index.get_meta("m1")["confidence"] == "low"
        hits = index.search_dense(_vec(2), k=5)
        assert [h[0] for h in hits] == ["m1"]

    def test_wrong_vector_dim_rejected(self, index, entry_factory):
        with pytest.raises(ValueError, match="维度"):
            index.upsert(entry_factory(entry_id="bad"), [0.1] * 10)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_vector_rejected(self, index, entry_factory, bad):
        vector = _vec(0)
        vector[0] = bad
        with pytest.raises(ValueError, match="NaN 或 Inf"):
            index.upsert(entry_factory(entry_id="bad"), vector)

    def test_zero_vector_rejected(self, index, entry_factory):
        with pytest.raises(ValueError, match="范数"):
            index.upsert(entry_factory(entry_id="zero"), [0.0] * EMBEDDING_DIM)

    def test_delete(self, index, entry_factory):
        index.upsert(entry_factory(entry_id="m1"), _vec(0))
        index.delete("m1")
        assert index.count() == 0
        assert index.search_dense(_vec(0), k=5) == []
        with pytest.raises(KeyError):
            index.delete("m1")


class TestSearch:
    @pytest.fixture
    def populated(self, index, entry_factory):
        index.upsert(entry_factory(entry_id="g1", content="用户用 uv 管理环境"), _vec(0))
        index.upsert(
            entry_factory(entry_id="r1", scope="repo:proj", content="本项目 dev server 端口 8765"),
            _vec(1),
        )
        index.upsert(
            entry_factory(entry_id="r2", scope="repo:other", content="其他仓库的记忆"),
            _vec(0),
        )
        return index

    def test_dense_scope_filter(self, populated):
        # 不过滤：k=2 时两条与查询同向的向量（距离 0）排最前
        hits = populated.search_dense(_vec(0), k=2)
        assert {h[0] for h in hits} == {"g1", "r2"}
        # repo:proj + global：r2（repo:other）被过滤，剩下 g1（距离 0）和 r1（距离 1）
        hits = populated.search_dense(_vec(0), k=5, scopes=["repo:proj", "global"])
        assert [h[0] for h in hits] == ["g1", "r1"]

    def test_dense_cosine_ranking(self, populated):
        hits = populated.search_dense(_vec(1), k=3, scopes=["repo:proj", "global"])
        assert hits[0][0] == "r1"  # 同向向量距离最小
        assert hits[0][1] < 1e-6

    def test_sparse_chinese_trigram(self, populated):
        # trigram 分词支持中文子串匹配（查询需 ≥3 字符）
        hits = populated.search_sparse("uv 管理环境", k=5)
        assert [h[0] for h in hits] == ["g1"]
        hits = populated.search_sparse("8765", k=5, scopes=["repo:proj", "global"])
        assert [h[0] for h in hits] == ["r1"]
        # 不存在的词不命中
        assert populated.search_sparse("PostgreSQL", k=5) == []

    def test_sparse_matches_natural_language_question_via_terms(self, index, entry_factory):
        """自然语言长问句要靠词项命中。

        回归：稀疏路原先只用整句短语做 trigram 匹配，等价于整句子串匹配，
        于是长问句恒为 0 命中、混合检索退化成纯向量。
        """
        index.upsert(
            entry_factory(entry_id="g1", content="The guitar was serviced at the shop on Main St."),
            _vec(0),
        )
        hits = index.search_sparse("Where did I get my guitar serviced?", k=5)
        assert [h[0] for h in hits] == ["g1"]

    def test_sparse_whole_phrase_still_ranks_first(self, index, entry_factory):
        """整句短语命中优先于只命中单个词项的条目。"""
        exact = entry_factory(entry_id="exact", content="用户用 uv 管理环境，不用 pip")
        index.upsert(exact, _vec(0))
        index.upsert(entry_factory(entry_id="partial", content="环境变量统一放在 .env"), _vec(1))
        hits = index.search_sparse("uv 管理环境", k=5)
        assert hits[0][0] == "exact"

    def test_sparse_unrelated_question_still_empty(self, index, entry_factory):
        """分词不能把不相关的问句也变成命中——词项一个都不在库里就该是空。"""
        index.upsert(entry_factory(entry_id="g1", content="用户用 uv 管理环境"), _vec(0))
        assert index.search_sparse("How much does PostgreSQL licensing cost?", k=5) == []

    def test_sparse_respects_k_across_expressions(self, index, entry_factory):
        """多表达式合并后仍然不超过 k，且不重复返回同一条目。"""
        for i in range(6):
            index.upsert(
                entry_factory(entry_id=f"m{i}", content=f"guitar serviced note {i}"), _vec(i)
            )
        hits = index.search_sparse("guitar serviced", k=3)
        assert len(hits) == 3
        assert len({h[0] for h in hits}) == 3

    def test_fts_indexes_detail_text(self, index, entry_factory):
        """FTS 索引的是 content + detail 拼接文本：只在 detail 出现的词也能命中。"""
        entry = entry_factory(entry_id="d1", content="用户偏好命令行工具。")
        entry = entry.model_copy(update={"detail": "这个偏好源于 wyvern 项目的构建约定。"})
        index.upsert(entry, _vec(0))
        assert [h[0] for h in index.search_sparse("wyvern", k=5)] == ["d1"]

    def test_rebuild_embeds_index_text(self, tmp_path, entry_factory):
        """rebuild 时向量嵌入的输入是 content + detail 拼接文本。"""

        class RecordingEmbedder:
            def __init__(self):
                self.texts: list[str] = []

            def embed_texts(self, texts):
                self.texts.extend(texts)
                return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]

        store = MarkdownStore(tmp_path)
        entry = entry_factory(entry_id="e1", content="原子句。")
        store.create(entry.model_copy(update={"detail": "完整段落。"}))
        rec = RecordingEmbedder()
        index = IndexDB(tmp_path / "index.db")
        try:
            index.rebuild_from_markdown(store.memory_dir, rec)
        finally:
            index.close()
        assert rec.texts == ["原子句。\n完整段落。"]


class TestRebuild:
    def test_rebuild_from_markdown(self, tmp_path, entry_factory, fake_embedder):
        store = MarkdownStore(tmp_path)
        store.create(entry_factory(entry_id="e1", content="用户用 uv 管理环境"))
        store.create(
            entry_factory(entry_id="e2", scope="repo:p", content="本项目 dev server 端口 8765")
        )
        index = IndexDB(tmp_path / "index.db")
        try:
            assert index.rebuild_from_markdown(store.memory_dir, fake_embedder) == 2
            assert index.count() == 2
            assert [h[0] for h in index.search_sparse("8765", k=5)] == ["e2"]
        finally:
            index.close()

    def test_rebuild_is_idempotent(self, tmp_path, entry_factory, fake_embedder):
        store = MarkdownStore(tmp_path)
        for i in range(3):
            store.create(entry_factory(entry_id=f"e{i}", content=f"第 {i} 条 uv 记忆"))
        index = IndexDB(tmp_path / "index.db")
        try:
            index.rebuild_from_markdown(store.memory_dir, fake_embedder)
            first = index.search_dense(fake_embedder.embed_texts(["uv"])[0], k=10)
            index.rebuild_from_markdown(store.memory_dir, fake_embedder)
            second = index.search_dense(fake_embedder.embed_texts(["uv"])[0], k=10)
            assert index.count() == 3
            assert first == second  # 重建两次结果一致
        finally:
            index.close()

    def test_rebuild_clears_stale_rows(self, tmp_path, entry_factory, fake_embedder):
        store = MarkdownStore(tmp_path)
        store.create(entry_factory(entry_id="e1", content="uv 记忆"))
        index = IndexDB(tmp_path / "index.db")
        try:
            index.rebuild_from_markdown(store.memory_dir, fake_embedder)
            store.delete("e1")
            store.create(entry_factory(entry_id="e2", content="新的端口记忆"))
            index.rebuild_from_markdown(store.memory_dir, fake_embedder)
            assert index.count() == 1
            assert index.get_meta("e1") is None
        finally:
            index.close()
