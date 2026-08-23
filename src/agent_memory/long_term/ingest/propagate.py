"""变更传播（反向传播）：UPDATE/DELETE 落库后，检查语义上依赖旧事实的既有条目。

要解决的通病：写入侧的 UPDATE/DELETE 只影响目标条目本身，而旧事实可能
是其它既有记忆（尤其是 procedural 操作约定）的前提——例如"功能开关已
全量删除"取代了"开关灰度中"，但"排查问题先看开关状态"的 procedural
记忆没有任何机制感知到前提失效，召回时仍会给出过期建议。

机制（reconcile 每次 UPDATE/DELETE 后调用）：
1. 以被取代/被删除的旧条目为 query，混合检索 + 稠密距离阈值找语义近邻；
   procedural 条目全量并入判定——操作约定依赖事实前提，措辞差异大，
   纯距离阈值容易漏；
2. 逐条交 LLM 三分类判定：
   - INVALIDATED：核心前提已失效 → 删除，并向 data/logs/propagation.jsonl
     追加审计记录（含被删条目完整快照，即版本历史）；
   - NEEDS_REVISION：受影响但机器没有足够证据改写 → 进 review_queue
     人工复核，不强行收敛（书第 3 章：保留待确认状态）；
   - UNAFFECTED：不动；
3. 只传播一跳，不级联（避免级联删除风暴；级联由 M4 整理循环负责）。

fail-safe：LLM 判定失败或输出无法解析时不动作、进复核队列，宁可排队
等人也不让坏决策落库（与 reconcile 的 CONFLICT 策略一致）。

注意：judge_propagation 的语义是"某条记忆被撤销后牵连谁"，不能用来回答
"这条记忆本身还成立吗"——M4 离线抽查复核用本模块的 judge_validity
（语义独立：默认假设成立，只有明确证据才判失效，证据不足交人工）。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_memory.config import Settings
from agent_memory.llm import LLMClient, LLMError
from agent_memory.long_term.ingest.review_queue import write_review_queue
from agent_memory.long_term.retrieve.hybrid import HybridSearcher
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import MemoryEntry

# 传播近邻的检索条数与稠密距离阈值（与 reconcile 的近邻标准一致：
# cosine 距离 ≤ 0.35 才算语义近邻，值得花一次 LLM 判定）
PROPAGATION_TOP_K = 5
PROPAGATION_MAX_DISTANCE = 0.35
# 取稠密距离时放大检索面，避免 top_k 之外的近邻拿不到距离
_DISTANCE_LOOKUP_K = 50
# 传播审计日志（追加式 JSONL）：INVALIDATED 删除的条目完整快照留在这里
AUDIT_LOG_NAME = "propagation.jsonl"

PROPAGATION_VERDICTS = {"INVALIDATED", "NEEDS_REVISION", "UNAFFECTED"}

_SCHEMA_DESCRIPTION = """{
  "verdict": "INVALIDATED | NEEDS_REVISION | UNAFFECTED",
  "reason": "一句话依据"
}"""

_SYSTEM_PROMPT = (
    "你是记忆库的传播判定员。记忆库里刚发生了一次变更：一条旧事实被新事实"
    "取代（或删除）。给你旧事实、新事实和一条既有记忆，你要判定这条既有"
    "记忆是否因该变更而失效：\n"
    "\n"
    "- INVALIDATED：既有记忆的核心前提就是旧事实，旧事实失效后它整体不再"
    "成立，保留它会误导未来协作（典型：基于旧状态的操作建议、排查步骤、"
    "灰度期约定）。判定要克制：仅仅提到同一主题不算失效，必须是核心前提"
    "被推翻；\n"
    "- NEEDS_REVISION：既有记忆部分受变更影响，需要修订措辞或补充例外，"
    "但你没有足够信息确定该怎么改——交人工复核，不要擅自改写；\n"
    "- UNAFFECTED：既有记忆与变更无关，或提到的内容在新事实下依然成立。\n"
    "\n"
    "判断依据只看事实依赖关系，不要被措辞相似迷惑。"
)

_USER_TEMPLATE = """记忆库刚发生的变更：
- 旧事实（已失效/被取代）：{old_content}
{new_clause}

待判定的既有记忆：
- id: {neighbor_id}
- 类型: {neighbor_type}
- 内容: {neighbor_content}

请按系统要求的 JSON 结构输出你的判定。"""


@dataclass
class PropagationReport:
    """一次变更的传播结果统计。"""

    invalidated: list[str] = field(default_factory=list)  # 判失效并已删除的近邻 id
    needs_revision: list[str] = field(default_factory=list)  # 判需修订、进复核队列的近邻 id
    unaffected: int = 0  # 判不受影响的条数
    failed: list[tuple[str, str]] = field(default_factory=list)  # (近邻 id, 失败原因)
    queued_files: list[Path] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "propagated_invalidate": len(self.invalidated),
            "propagated_revise": len(self.needs_revision),
            "propagated_unaffected": self.unaffected,
            "propagated_failed": len(self.failed),
        }


def judge_propagation(
    llm: LLMClient,
    old: MemoryEntry,
    new: MemoryEntry | None,
    neighbor: MemoryEntry,
) -> dict:
    """判定单条既有记忆是否因一次变更（old -> new / 删除）而失效。

    返回 {"verdict": ..., "reason": ...}。LLMError 向上抛，由调用方决定
    如何兜底；输出无法解析时按 UNAFFECTED 处理（fail-safe：宁可不动也
    不错删）。
    """
    new_clause = (
        f"- 新事实（当前有效）：{new.content}"
        if new is not None
        else "- 旧事实已被删除，没有替代的新事实"
    )
    parsed = llm.complete_json(
        system=_SYSTEM_PROMPT,
        user=_USER_TEMPLATE.format(
            old_content=old.content,
            new_clause=new_clause,
            neighbor_id=neighbor.id,
            neighbor_type=neighbor.memory_type,
            neighbor_content=neighbor.content,
        ),
        schema_description=_SCHEMA_DESCRIPTION,
    )
    verdict = str(parsed.get("verdict", "")).upper()
    if verdict not in PROPAGATION_VERDICTS:
        verdict = "UNAFFECTED"
    return {"verdict": verdict, "reason": str(parsed.get("reason", ""))}


VALIDITY_VERDICTS = {"VALID", "INVALIDATED", "NEEDS_REVISION"}

_VALIDITY_SCHEMA_DESCRIPTION = """{
  "verdict": "VALID | INVALIDATED | NEEDS_REVISION",
  "reason": "一句话依据"
}"""

_VALIDITY_SYSTEM_PROMPT = (
    "你是记忆库的离线复核员。给你一条既有记忆（内容、置信度、最后核实时间、"
    "证据指针）和库中与它语义最相近的若干条目，判断这条记忆本身是否仍然"
    "成立：\n"
    "\n"
    "默认假设它成立（VALID）。只有存在明确证据时才判失效：\n"
    "- INVALIDATED：有明确证据表明它已不再成立——被近邻中更新的条目取代"
    "（对同一事实给出了更新的取值）、与近邻条目直接矛盾且对方更近期或证据"
    "更充分、或内容自身含已过期的时间条件（如“灰度期间”“本周内”而时间"
    "已过）；\n"
    "- NEEDS_REVISION：你怀疑它过期或部分失效，但现有证据不足以确认——"
    "交人工复核，不要直接判失效；\n"
    "- VALID：没有明确失效证据。last_verified 较旧本身不是失效证据。\n"
    "\n"
    "判断依据只看事实关系，不要被措辞相似迷惑；宁可留待人工也不误删正常记忆。"
)

_VALIDITY_USER_TEMPLATE = """待复核的记忆：
- id: {entry_id}
- 类型: {entry_type}
- 置信度: {entry_confidence}
- 最后核实: {entry_verified}
- 证据数: {entry_evidence}
- 内容: {entry_content}

库中语义最相近的既有条目（供对照，可能为空）：
{neighbors_text}

请按系统要求的 JSON 结构输出你的判定。"""


def judge_validity(
    llm: LLMClient,
    entry: MemoryEntry,
    neighbors: list[MemoryEntry],
) -> dict:
    """离线抽查复核：判断一条既有记忆本身是否仍然成立（M4 整理循环用）。

    与 judge_propagation 的语义边界：propagation 回答"某条记忆被撤销后
    牵连谁"，本函数回答"这条记忆本身还成立吗"。默认假设成立，只有明确
    证据（被更新条目取代 / 与现存条目矛盾 / 内容含已过期时间条件）才判
    INVALIDATED；证据不足以确认时判 NEEDS_REVISION 交人工，而不是判失效。

    返回 {"verdict": ..., "reason": ...}。LLMError 向上抛；输出无法解析
    时按 VALID 处理（fail-safe：默认不动，宁可漏判也不错删）。
    """
    neighbors_text = "\n".join(
        f"- id: {n.id}（最后核实: {n.last_verified.isoformat()}）内容: {n.content}"
        for n in neighbors
    ) or "（无）"
    parsed = llm.complete_json(
        system=_VALIDITY_SYSTEM_PROMPT,
        user=_VALIDITY_USER_TEMPLATE.format(
            entry_id=entry.id,
            entry_type=entry.memory_type,
            entry_confidence=entry.confidence,
            entry_verified=entry.last_verified.isoformat(),
            entry_evidence=len(entry.evidence),
            entry_content=entry.content,
            neighbors_text=neighbors_text,
        ),
        schema_description=_VALIDITY_SCHEMA_DESCRIPTION,
    )
    verdict = str(parsed.get("verdict", "")).upper()
    if verdict not in VALIDITY_VERDICTS:
        verdict = "VALID"
    return {"verdict": verdict, "reason": str(parsed.get("reason", ""))}


def find_dependents(
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    settings: Settings,
    old: MemoryEntry,
    exclude_ids: set[str],
) -> list[MemoryEntry]:
    """找出语义上可能依赖旧事实的既有条目：距离阈值内的近邻 ∪ 同 scope 全部 procedural。"""
    searcher = HybridSearcher(store, index, embedder, settings)
    results = searcher.search(old.content, scopes=[old.scope], k=PROPAGATION_TOP_K)
    vector = embedder.embed_texts([old.index_text])[0]
    distances = dict(
        index.search_dense(vector, k=_DISTANCE_LOOKUP_K, scopes=[old.scope, "global"])
    )
    dependents: dict[str, MemoryEntry] = {}
    for r in results:
        if r.entry.id in exclude_ids:
            continue
        if distances.get(r.entry.id, 1.0) <= PROPAGATION_MAX_DISTANCE:
            dependents[r.entry.id] = r.entry
    # procedural 全量并入：操作约定的前提依赖常被措辞差异掩盖，距离阈值会漏
    for entry in store.list(scope=old.scope):
        if entry.memory_type == "procedural" and entry.id not in exclude_ids:
            dependents.setdefault(entry.id, entry)
    return list(dependents.values())


def _append_audit(
    data_dir: Path, old: MemoryEntry, new: MemoryEntry | None, removed: MemoryEntry, reason: str
) -> None:
    """向 data/logs/propagation.jsonl 追加一条失效删除的审计记录（含完整快照）。"""
    logs_dir = Path(data_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event": "propagated_invalidation",
        "at": datetime.now().isoformat(timespec="seconds"),
        "trigger_old": old.model_dump(mode="json"),
        "trigger_new": new.model_dump(mode="json") if new is not None else None,
        "removed": removed.model_dump(mode="json"),
        "reason": reason,
    }
    with (logs_dir / AUDIT_LOG_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def propagate_change(
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    settings: Settings,
    llm: LLMClient,
    *,
    old: MemoryEntry,
    new: MemoryEntry | None,
    exclude_ids: set[str] | None = None,
    data_dir: Path | None = None,
) -> PropagationReport:
    """一次 UPDATE/DELETE 的反向传播主流程。只传播一跳，不级联。

    old：被取代/被删除的旧条目（传播 query）；new：取代它的新条目
    （DELETE 无新事实时为 None）；exclude_ids：不参与判定的 id
    （变更双方自身）；data_dir：复核队列与审计日志根目录，缺省取
    store.data_dir。
    """
    data_dir = Path(data_dir or store.data_dir)
    excluded = set(exclude_ids or ()) | {old.id}
    if new is not None:
        excluded.add(new.id)
    report = PropagationReport()

    for neighbor in find_dependents(store, index, embedder, settings, old, excluded):
        try:
            verdict = judge_propagation(llm, old, new, neighbor)
        except LLMError as e:
            # 判定失败不动作，进复核队列（fail-safe，可见）
            report.failed.append((neighbor.id, f"传播判定不可用：{e}"))
            continue
        if verdict["verdict"] == "INVALIDATED":
            store.delete(neighbor.id)
            index.delete(neighbor.id)
            _append_audit(data_dir, old, new, neighbor, verdict["reason"])
            report.invalidated.append(neighbor.id)
        elif verdict["verdict"] == "NEEDS_REVISION":
            report.needs_revision.append(neighbor.id)
        else:
            report.unaffected += 1

    queued: list[MemoryEntry] = []
    for neighbor_id in report.needs_revision:
        queued.append(store.get(neighbor_id))
    if queued:
        report.queued_files.extend(
            write_review_queue(
                queued,
                data_dir,
                reason=f"变更传播判定需修订（变更来源：{old.id} 被取代/删除）",
            )
        )
    if report.failed:
        failed_entries = [store.get(nid) for nid, _ in report.failed]
        report.queued_files.extend(
            write_review_queue(
                failed_entries, data_dir, reason="变更传播判定失败，需人工确认是否受影响"
            )
        )
    return report
