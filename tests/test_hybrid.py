"""hybrid 检索的排序逻辑：RRF 融合、置信度权重、时间衰减（构造数据，不依赖真模型）。"""

from datetime import date, timedelta

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.retrieve.hybrid import (
    CANDIDATE_TOP_N,
    DECAY_FLOOR,
    RRF_K,
    HybridSearcher,
    confidence_weight,
    rrf_fuse,
    time_decay,
)

TODAY = date(2026, 8, 19)


class TestConfidenceWeight:
    def test_mapping(self):
        assert confidence_weight("high") == 1.0
        assert confidence_weight("medium") == 0.8
        assert confidence_weight("low") == 0.6


class TestTimeDecay:
    def test_fresh_memory_no_decay(self):
        assert time_decay(TODAY, TODAY, 90) == 1.0
        assert time_decay(TODAY - timedelta(days=90), TODAY, 90) == 1.0

    def test_linear_decay_after_stale_days(self):
        # 超过 stale_days 后线性衰减：中点 (90+180)/2=135 天时应为 0.75
        assert time_decay(TODAY - timedelta(days=135), TODAY, 90) == pytest.approx(0.75)

    def test_floor(self):
        # 2×stale_days 时到下限 0.5，更旧也不再降
        assert time_decay(TODAY - timedelta(days=180), TODAY, 90) == DECAY_FLOOR
        assert time_decay(TODAY - timedelta(days=3650), TODAY, 90) == DECAY_FLOOR

    def test_stale_days_zero_no_crash(self):
        assert time_decay(TODAY, TODAY, 0) == 1.0
        assert time_decay(TODAY - timedelta(days=10), TODAY, 0) == DECAY_FLOOR


class TestRrfFuse:
    def test_scores_and_ranks(self):
        fused = rrf_fuse(["a", "b", "c"], ["b"])
        assert fused["a"]["dense_rank"] == 1 and fused["a"]["sparse_rank"] is None
        assert fused["b"]["dense_rank"] == 2 and fused["b"]["sparse_rank"] == 1
        assert fused["c"]["sparse_rank"] is None
        # 两路都命中的 b 总分最高；只命中稠密且靠后的 c 最低
        assert fused["b"]["rrf_score"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
        assert fused["a"]["rrf_score"] == pytest.approx(1 / (RRF_K + 1))
        assert fused["b"]["rrf_score"] > fused["a"]["rrf_score"] > fused["c"]["rrf_score"]


class TestHybridSearchRanking:
    """用 FakeEmbedder + 真实 store/index 构造数据，验证综合排序。"""

    @pytest.fixture
    def env(self, tmp_path, store, index, entry_factory, fake_embedder):
        def add(entry):
            store.create(entry)
            index.upsert(entry, fake_embedder.embed_texts([entry.content])[0])

        # 三条都含 "uv"（向量同向、FTS 都命中），RRF 分相同，排序完全由权重决定
        add(entry_factory(entry_id="fresh-high", content="用户用 uv 管理环境"))
        add(
            entry_factory(
                entry_id="fresh-medium",
                content="用户用 uv 安装依赖",
                confidence="medium",
            )
        )
        add(
            entry_factory(
                entry_id="stale-high",
                content="用户用 uv 同步环境",
                last_verified=date.today() - timedelta(days=180),
            )
        )
        searcher = HybridSearcher(store, index, fake_embedder, Settings())
        return searcher

    def test_confidence_and_decay_shape_ranking(self, env):
        results = env.search("uv", k=10)
        ranking = {r.entry.id: r.score for r in results}
        # high(1.0) > medium(0.8) > stale-high(0.5)，前提 RRF 分相同（同向向量+同关键词）
        assert ranking["fresh-high"] > ranking["fresh-medium"]
        assert ranking["fresh-medium"] > ranking["stale-high"]
        assert results[0].entry.id == "fresh-high"

    def test_result_carries_full_entry_and_matched_text(self, env):
        r = env.search("uv", k=1)[0]
        assert r.entry.id == "fresh-high"
        assert r.matched_text == r.entry.content  # 命中文本是原始证据，不做摘要
        assert r.rrf_score > 0

    def test_scope_filtering_includes_global(self, tmp_path, store, index, entry_factory,
                                             fake_embedder):
        store.create(entry_factory(entry_id="g", content="全局 uv 记忆"))
        store.create(
            entry_factory(entry_id="mine", scope="repo:mine", content="本仓库 uv 记忆")
        )
        store.create(
            entry_factory(entry_id="theirs", scope="repo:other", content="别的仓库 uv 记忆")
        )
        for e in store.list():
            index.upsert(e, fake_embedder.embed_texts([e.content])[0])
        searcher = HybridSearcher(store, index, fake_embedder, Settings())
        ids = {r.entry.id for r in searcher.search("uv", scopes=["repo:mine"])}
        assert ids == {"g", "mine"}  # 当前 scope + global，排除 repo:other

    def test_rerank_explicitly_enabled_raises(self, env):
        with pytest.raises(NotImplementedError):
            env.search("uv", rerank=True)

    def test_track_retrieval_increments_hit_counts(self, env):
        """track_retrieval=True 时返回条目的 retrieval_count +1（M4a）。"""
        env.search("uv", k=2, track_retrieval=True)
        assert env.store.get("fresh-high").retrieval_count == 1
        assert env.store.get("fresh-medium").retrieval_count == 1
        assert env.store.get("stale-high").retrieval_count == 0  # 未进 top-k 不计
        env.search("uv", k=1, track_retrieval=True)
        assert env.store.get("fresh-high").retrieval_count == 2

    def test_default_search_does_not_track(self, env):
        """默认不计数：写路径的近邻检索（reconcile/propagate）不是用户检索。"""
        env.search("uv", k=10)
        assert env.store.get("fresh-high").retrieval_count == 0


class TestCandidatePoolScalesWithK:
    """回归：候选池固定 20 时，k>20 的调用永远拿不到更多条目。

    AML Search 契约要 top_k=100，记忆层却只能返回约 20 条，剩下的位置被浪费。
    """

    @pytest.fixture
    def many(self, store, index, fake_embedder, entry_factory):
        for i in range(60):
            entry = entry_factory(entry_id=f"m{i:02d}", content=f"uv 相关记忆第 {i} 条")
            store.create(entry)
            index.upsert(entry, fake_embedder.embed_texts([entry.content])[0])
        return HybridSearcher(store, index, fake_embedder, Settings())

    def test_k_above_candidate_floor_returns_more_than_floor(self, many):
        assert len(many.search("uv", scopes=["global"], k=40)) > CANDIDATE_TOP_N

    def test_small_k_unchanged(self, many):
        assert len(many.search("uv", scopes=["global"], k=10)) == 10


class TestDenseDistanceCarried:
    def test_dense_hit_carries_cosine_distance(self, store, index, fake_embedder, entry_factory):
        entry = entry_factory(entry_id="d1", content="用户用 uv 管理环境")
        store.create(entry)
        index.upsert(entry, fake_embedder.embed_texts([entry.content])[0])
        searcher = HybridSearcher(store, index, fake_embedder, Settings())
        (hit,) = searcher.search("uv", scopes=["global"], k=5)
        assert hit.dense_distance is not None
        assert 0.0 <= hit.dense_distance <= 2.0

    def test_sparse_only_hit_has_no_distance(self, store, index, fake_embedder, entry_factory):
        """只命中稀疏路的条目没有 cosine 距离——置信度估计要能区分这种情况。"""
        entry = entry_factory(entry_id="s1", content="PostgreSQL 连接串放在 secrets 里")
        store.create(entry)
        # 故意写入一个与任何查询都不同向的向量，让稠密路不会把它选进候选
        vector = [0.0] * 1024
        vector[1023] = 1.0
        index.upsert(entry, vector)
        other = entry_factory(entry_id="o1", content="用户用 uv 管理环境")
        store.create(other)
        index.upsert(other, fake_embedder.embed_texts([other.content])[0])
        searcher = HybridSearcher(store, index, fake_embedder, Settings())
        hits = {r.entry.id: r for r in searcher.search("PostgreSQL 连接串", scopes=["global"], k=1)}
        assert "s1" in hits
        assert hits["s1"].sparse_rank is not None
