"""整理循环的触发判定（五步循环第一步：触发）。

满足任一条件即触发：
1. 距上次整理超过 settings.evolve_interval_days 天（从未运行过视为必触发）；
2. 上次整理以来新增条目数超过 settings.evolve_new_entries_threshold；
3. review_queue 积压（直接子级的 .yaml 待办数）超过
   settings.evolve_review_backlog_threshold。

全部阈值在 config.py（AGENT_MEMORY_EVOLVE_* 环境变量可覆盖）。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_memory.config import Settings
from agent_memory.store.markdown_store import MarkdownStore


@dataclass
class StoreStats:
    """触发判定的输入快照。"""

    total_entries: int  # 记忆层总条目数
    new_entries_since_last_run: int  # 上次整理以来新增（按 created_at 计）的条目数
    review_queue_backlog: int  # review_queue 直接子级的待办文件数（不含 evolution/ 子目录）


def collect_store_stats(
    store: MarkdownStore, data_dir: Path, last_run_at: datetime | None
) -> StoreStats:
    """从记忆层与复核队列采集触发判定所需的统计。"""
    entries = store.list()
    if last_run_at is None:
        new_entries = len(entries)
    else:
        last_date = last_run_at.date()
        new_entries = sum(1 for e in entries if e.created_at > last_date)
    queue_dir = Path(data_dir) / "review_queue"
    backlog = (
        sum(1 for f in queue_dir.glob("*.yaml") if f.is_file()) if queue_dir.exists() else 0
    )
    return StoreStats(
        total_entries=len(entries),
        new_entries_since_last_run=new_entries,
        review_queue_backlog=backlog,
    )


def should_run(
    stats: StoreStats,
    last_run_at: datetime | None,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """判定是否该跑一次整理循环，返回 (是否触发, 原因)。"""
    now = now or datetime.now()
    reasons: list[str] = []
    if last_run_at is None:
        reasons.append("从未运行过整理循环")
    else:
        elapsed = (now - last_run_at).days
        if elapsed >= settings.evolve_interval_days:
            reasons.append(
                f"距上次整理已 {elapsed} 天（阈值 {settings.evolve_interval_days} 天）"
            )
    if stats.new_entries_since_last_run >= settings.evolve_new_entries_threshold:
        reasons.append(
            f"新增条目 {stats.new_entries_since_last_run} 条"
            f"（阈值 {settings.evolve_new_entries_threshold}）"
        )
    if stats.review_queue_backlog >= settings.evolve_review_backlog_threshold:
        reasons.append(
            f"复核队列积压 {stats.review_queue_backlog} 条"
            f"（阈值 {settings.evolve_review_backlog_threshold}）"
        )
    if reasons:
        return True, "；".join(reasons)
    return False, "未达到任何触发条件"
