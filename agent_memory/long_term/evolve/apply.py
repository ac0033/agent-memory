"""晋升与回滚（五步循环最后一步：修剪落库）。

硬边界与审计：
- apply 前对记忆层做快照（data/snapshots/<timestamp>/memory 完整拷贝）；
- apply 只执行机器可决策的变更（merge / invalidate / downgrade / archive），
  conflict / revise 是人工裁决项，一律跳过；
- 应用后向 data/logs/evolution_audit.jsonl 追加审计记录（提案 id、三档验证
  结果、应用时间、快照路径、逐条变更落库情况）；
- rollback(snapshot_id) 从快照恢复记忆层并重建索引。

evolution_audit.jsonl 是可信根（红线 D6），禁止 agent 自修改。
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_memory.config import Settings
from agent_memory.io_utils import atomic_write_text, interprocess_lock
from agent_memory.long_term.store.coordinator import MemoryWriter
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import EvolutionChange, EvolutionProposal

AUDIT_LOG_NAME = "evolution_audit.jsonl"


@dataclass
class ApplyReport:
    """一次晋升的结果。"""

    applied: list[tuple[str, list[str]]] = field(default_factory=list)  # (kind, target_ids)
    skipped: list[tuple[str, list[str], str]] = field(default_factory=list)  # (kind, ids, 原因)
    snapshot_id: str | None = None
    audit_path: Path | None = None


def snapshot_memory(data_dir: Path, now: datetime | None = None) -> tuple[str, Path]:
    """把 data/memory 完整拷贝到 data/snapshots/<timestamp>/memory，返回 (snapshot_id, 路径)。"""
    now = now or datetime.now()
    snapshot_id = f"{now:%Y%m%dT%H%M%S}"
    dest = Path(data_dir) / "snapshots" / snapshot_id / "memory"
    dest.parent.mkdir(parents=True, exist_ok=True)
    memory_dir = Path(data_dir) / "memory"
    if memory_dir.exists():
        shutil.copytree(memory_dir, dest)
    else:
        dest.mkdir(parents=True)
    return snapshot_id, dest


def _apply_changes(
    changes: list[EvolutionChange],
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    report: ApplyReport,
) -> None:
    """逐条应用变更。conflict / revise 跳过（人工裁决项）。"""
    # apply_proposal owns the outer durable evolution transaction and recovery marker.
    writer = MemoryWriter(store, index, embedder, recover=False)
    for change in changes:
        if change.kind in {"conflict", "revise"}:
            report.skipped.append((change.kind, change.target_ids, "人工裁决项，不自动应用"))
            continue
        if change.kind == "merge":
            merged = change.merged_entry
            assert merged is not None  # schema 校验保证
            # 先让合并结果完整落地，再协调删除来源；外层 apply_proposal 对整批
            # 任何异常使用只读快照自动恢复。
            writer.create(merged)
            for tid in change.target_ids:
                writer.delete(tid)
            report.applied.append(("merge", change.target_ids))
        elif change.kind in {"invalidate", "archive"}:
            for tid in change.target_ids:
                writer.delete(tid)
            report.applied.append((change.kind, change.target_ids))
        elif change.kind == "downgrade":
            for tid in change.target_ids:
                entry = store.get(tid)
                writer.update(
                    entry.model_copy(update={"confidence": change.new_confidence})
                )
            report.applied.append(("downgrade", change.target_ids))


def apply_proposal(
    proposal: EvolutionProposal,
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    settings: Settings,
    verify_report=None,
    now: datetime | None = None,
) -> ApplyReport:
    """晋升一个已通过验证的提案：快照 → 应用 → 审计。

    verify_report 传入且未通过时 fail-closed 拒绝应用（三档 veto 不可绕过）。
    """
    from agent_memory.long_term.evolve.verify import TierResult, VerifyReport

    if not isinstance(verify_report, VerifyReport):
        raise ValueError(f"提案 {proposal.id} 缺少真实三档验证报告，拒绝晋升（fail-closed）")
    tiers = (verify_report.boundary, verify_report.retention, verify_report.safety)
    if not all(
        isinstance(tier, TierResult) and type(tier.passed) is bool and tier.passed
        for tier in tiers
    ):
        raise ValueError(f"提案 {proposal.id} 未通过三档验证，拒绝晋升（fail-closed）")
    now = now or datetime.now()
    # Snapshot, apply, rollback and audit form one exclusive evolution unit. Normal
    # writes wait outside this lock, so a failed snapshot rollback cannot erase a
    # concurrently committed memory.
    with interprocess_lock(store.lock_path):
        snapshot_id, snapshot_path = snapshot_memory(settings.data_dir, now)
        evolution_journal = (
            Path(settings.data_dir) / "state" / "evolution_apply_journal.json"
        )
        atomic_write_text(
            evolution_journal,
            json.dumps(
                {
                    "version": 1,
                    "proposal_id": proposal.id,
                    "snapshot_id": snapshot_id,
                    "started_at": now.isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        report = ApplyReport(snapshot_id=snapshot_id)
        try:
            _apply_changes(proposal.changes, store, index, embedder, report)
            logs_dir = Path(settings.data_dir) / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            audit_path = logs_dir / AUDIT_LOG_NAME
            record = {
                "event": "evolution_applied",
                "proposal_id": proposal.id,
                "applied_at": now.isoformat(timespec="seconds"),
                "snapshot_id": snapshot_id,
                "snapshot_path": str(snapshot_path),
                "verify": verify_report.to_dict(),
                "changes_applied": [
                    {"kind": kind, "target_ids": ids} for kind, ids in report.applied
                ],
                "changes_skipped": [
                    {"kind": kind, "target_ids": ids, "reason": reason}
                    for kind, ids, reason in report.skipped
                ],
            }
            with interprocess_lock(
                Path(settings.data_dir) / "state" / "evolution_audit.lock"
            ):
                with audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        except Exception:
            # snapshot 是可信根，只读复制回正式层；不改写快照本身。
            if store.memory_dir.exists():
                shutil.rmtree(store.memory_dir)
            shutil.copytree(snapshot_path, store.memory_dir)
            index.rebuild_from_markdown(store.memory_dir, embedder)
            (Path(settings.data_dir) / "state" / "memory_write_journal.json").unlink(
                missing_ok=True
            )
            evolution_journal.unlink(missing_ok=True)
            raise
        evolution_journal.unlink()
        report.audit_path = audit_path
        return report


def rollback(snapshot_id: str, settings: Settings, embedder) -> Path:
    """从快照恢复记忆层并重建索引，返回恢复的记忆层路径。

    恢复前把当前记忆层挪到 data/snapshots/<snapshot_id>/pre_rollback_memory
    保留现场（回滚本身也不是破坏性操作）；快照不存在时 fail-closed 报错。
    """
    data_dir = Path(settings.data_dir)
    snapshot_memory_dir = data_dir / "snapshots" / snapshot_id / "memory"
    if not snapshot_memory_dir.is_dir():
        raise FileNotFoundError(
            f"快照 {snapshot_id!r} 不存在：{snapshot_memory_dir}，无法回滚"
        )
    memory_dir = data_dir / "memory"
    if memory_dir.exists():
        backup = data_dir / "snapshots" / snapshot_id / "pre_rollback_memory"
        if backup.exists():
            shutil.rmtree(backup)
        memory_dir.rename(backup)
    shutil.copytree(snapshot_memory_dir, memory_dir)

    index = IndexDB(data_dir / "index.db")
    try:
        index.rebuild_from_markdown(memory_dir, embedder)
    finally:
        index.close()
    return memory_dir
