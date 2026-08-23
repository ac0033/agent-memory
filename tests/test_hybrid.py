"""hybrid 检索的排序逻辑：RRF 融合、置信度权重、时间衰减（构造数据，不依赖真模型）。"""

from datetime import date, timedelta

import pytest

from agent_memory.retrieve.hybrid import (
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
        from agent_memory.config import Settings

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
        from agent_memory.config import Settings

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
