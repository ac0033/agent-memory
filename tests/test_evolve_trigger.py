"""trigger.py 测试：三个触发条件（任一满足即触发）+ 未触发 + 统计采集。"""

from datetime import datetime, timedelta

from agent_memory.config import Settings
from agent_memory.long_term.evolve.trigger import StoreStats, collect_store_stats, should_run

NOW = datetime(2026, 8, 20, 12, 0, 0)


def _stats(new=0, backlog=0, total=0):
    return StoreStats(
        total_entries=total,
        new_entries_since_last_run=new,
        review_queue_backlog=backlog,
    )


class TestShouldRun:
    def test_never_run_triggers(self):
        triggered, reason = should_run(_stats(), None, Settings(), now=NOW)
        assert triggered
        assert "从未运行" in reason

    def test_interval_exceeded_triggers(self):
        last = NOW - timedelta(days=8)  # 默认阈值 7 天
        triggered, reason = should_run(_stats(), last, Settings(), now=NOW)
        assert triggered
        assert "8 天" in reason

    def test_interval_boundary_day_triggers(self):
        # 恰好等于阈值也触发（>= 语义）
        last = NOW - timedelta(days=7)
        triggered, _ = should_run(_stats(), last, Settings(), now=NOW)
        assert triggered

    def test_new_entries_threshold_triggers(self):
        last = NOW - timedelta(days=1)
        triggered, reason = should_run(_stats(new=50), last, Settings(), now=NOW)
        assert triggered
        assert "新增条目 50" in reason

    def test_review_backlog_threshold_triggers(self):
        last = NOW - timedelta(days=1)
        triggered, reason = should_run(_stats(backlog=10), last, Settings(), now=NOW)
        assert triggered
        assert "复核队列积压 10" in reason

    def test_below_all_thresholds_not_triggered(self):
        last = NOW - timedelta(days=1)
        triggered, reason = should_run(_stats(new=3, backlog=2), last, Settings(), now=NOW)
        assert not triggered
        assert "未达到" in reason

    def test_custom_thresholds_via_settings(self):
        settings = Settings(evolve_new_entries_threshold=5, evolve_interval_days=30)
        triggered, _ = should_run(_stats(new=5), NOW - timedelta(days=1), settings, now=NOW)
        assert triggered


class TestCollectStoreStats:
    def test_new_entries_counted_since_last_run(self, store, entry_factory, tmp_path):
        old = entry_factory(entry_id="e-old").model_copy(
            update={"created_at": (NOW - timedelta(days=10)).date()}
        )
        new = entry_factory(entry_id="e-new")  # created_at = 今天
        store.create(old)
        store.create(new)
        last_run = NOW - timedelta(days=1)
        stats = collect_store_stats(store, tmp_path, last_run)
        assert stats.total_entries == 2
        assert stats.new_entries_since_last_run == 1

    def test_never_run_counts_all_as_new(self, store, entry_factory, tmp_path):
        store.create(entry_factory(entry_id="e-1"))
        stats = collect_store_stats(store, tmp_path, None)
        assert stats.new_entries_since_last_run == 1

    def test_backlog_counts_top_level_yaml_only(self, store, tmp_path):
        queue = tmp_path / "review_queue"
        queue.mkdir(parents=True)
        (queue / "a.yaml").write_text("reason: x", encoding="utf-8")
        (queue / "b.yaml").write_text("reason: y", encoding="utf-8")
        # evolution/ 子目录里的提案不算积压待办
        evo = queue / "evolution" / "20260820T000000"
        evo.mkdir(parents=True)
        (evo / "proposal.yaml").write_text("id: p", encoding="utf-8")
        stats = collect_store_stats(store, tmp_path, None)
        assert stats.review_queue_backlog == 2

    def test_missing_queue_dir_is_zero(self, store, tmp_path):
        stats = collect_store_stats(store, tmp_path, None)
        assert stats.review_queue_backlog == 0
