"""对账（红线 D2 写入过门的第三道）：新候选与既有记忆的对齐决策（Mem0 式）。

每条候选的处理流程：
1. 在同 scope + global 里用混合检索找语义近邻（稠密距离阈值判定）；
2. 无近邻 → 直接 ADD；
3. 有近邻 → LLM 判 ADD / UPDATE / DELETE / NOOP；
4. LLM 判定为冲突且无法收敛（CONFLICT）、输出无法解析、或决策引用的目标
   不合法 → 不强行收敛，写入 data/review_queue 人工复核（fail-safe：
   宁可排队等人，也不让坏决策落库）。

版本化语义：UPDATE 产生的新条目 version = 旧条目 + 1、supersedes 指向旧 id，
旧条目从记忆层与索引同步删除（历史由新条目的 supersedes 指针与 evidence 承载）。

变更传播（反"变更不传播"通病）：UPDATE/DELETE 落库后调用
ingest.propagate.propagate_change——以被取代/删除的旧条目为 query 检索
语义近邻（procedural 全量并入），逐条 LLM 判定失效/需修订/不受影响；
判失效的删除并留审计日志（data/logs/propagation.jsonl），判需修订的进
复核队列。只传播一跳，不级联。
"""

from dataclasses import dataclass, field
from pathlib import Path

from agent_memory.config import Settings, get_settings
from agent_memory.ingest.gate import write_review_queue
from agent_memory.ingest.propagate import PropagationReport, propagate_change
from agent_memory.llm import LLMClient, LLMError
from agent_memory.models import MemoryEntry
from agent_memory.retrieve.embedder import get_embedder
from agent_memory.retrieve.hybrid import HybridSearcher
from agent_memory.store.index_db import IndexDB
from agent_memory.store.markdown_store import MarkdownStore, MemoryStoreError

# 语义近邻阈值：cosine 距离 ≤ 0.35（相似度 ≥ 0.65）才算"近邻"，需要 LLM 介入判决策
NEIGHBOR_MAX_DISTANCE = 0.35
# 近邻检索条数
NEIGHBOR_TOP_K = 5
# 取稠密距离时放大检索面，避免 hybrid top5 之外的近邻拿不到距离
_DISTANCE_LOOKUP_K = 50

ACTIONS = {"ADD", "UPDATE", "DELETE", "NOOP", "CONFLICT"}

_SCHEMA_DESCRIPTION = """{
  "action": "ADD | UPDATE | DELETE | NOOP | CONFLICT",
  "target_id": "UPDATE/DELETE/NOOP 时填既有记忆的 id；ADD 时填 null",
  "add_new": true/false,  // 仅 DELETE 用：删除旧条目后是否把新事实作为独立条目入库
  "reason": "一句话说明判断依据"
}"""

_SYSTEM_PROMPT = (
    "你是记忆库对账员。给你一条新记忆候选和若干条语义相近的既有记忆，"
    "你要判定这条候选该如何入库：\n"
    "\n"
    "- ADD：候选带来了既有记忆中没有的新信息，作为新条目直接入库。"
    "注意：主题相近不等于重复——候选与既有记忆说的是同一主题的不同侧面"
    "（如一条讲评审顺序、一条讲无测试的处理），彼此互为补充，判 ADD；\n"
    "- UPDATE：候选与某条既有记忆说的是同一事实，但取值被更新或修正"
    "（如时间从 10:00 改为 9:30、工具从 X 换成 Y、开关从开变为关），"
    "新条目将取代旧条目（版本 +1）。候选只是补充细节、没有取代旧取值时"
    "不是 UPDATE，判 ADD；\n"
    "- DELETE：候选提供的信息否定了某条既有记忆（旧事实已不再成立），删除旧条目；"
    "add_new 决定新事实本身是否还要作为独立条目入库；\n"
    "- NOOP：候选与某条既有记忆语义重复（没有新信息），不入库，只刷新旧条目的核实时间；\n"
    "- CONFLICT：候选与既有记忆对同一事实的取值相互矛盾，且双方各有证据、"
    "你无法凭时间先后或证据判断哪个成立——只有这种情况才 CONFLICT，交人工复核。"
    "互为补充的不同侧面不是冲突。\n"
    "\n"
    "判断依据只看事实关系，不要被措辞差异迷惑；时间/状态类事实以较新的日期为准判 UPDATE。"
)

_USER_TEMPLATE = """新记忆候选：
- id: {candidate_id}
- 内容: {candidate_content}
- 时间: {candidate_date}

语义相近的既有记忆：
{neighbors_text}

请按系统要求的 JSON 结构输出你的决策。"""


@dataclass
class ReconcileReport:
    """对账结果统计：五种处置各落了哪些条目。"""

    added: list[str] = field(default_factory=list)  # 新增入库的 id
    updated: list[tuple[str, str]] = field(default_factory=list)  # (新 id, 被取代的旧 id)
    deleted: list[str] = field(default_factory=list)  # 被 DELETE 删除的旧 id
    noops: list[str] = field(default_factory=list)  # 重复的候选 id（旧条目只刷新核实时间）
    queued: list[tuple[MemoryEntry, str]] = field(default_factory=list)  # (候选, 排队原因)
    queued_files: list[Path] = field(default_factory=list)
    # 变更传播结果：每次 UPDATE/DELETE 一份（见 ingest/propagate.py）
    propagation: list[PropagationReport] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {
            "add": len(self.added),
            "update": len(self.updated),
            "delete": len(self.deleted),
            "noop": len(self.noops),
            "queued": len(self.queued),
        }
        for p in self.propagation:
            for k, v in p.counts().items():
                out[k] = out.get(k, 0) + v
        return out


def _neighbors_text(neighbors: list[MemoryEntry]) -> str:
    return "\n".join(
        f"- id: {n.id}\n  内容: {n.content}\n  最后核实: {n.last_verified}"
        f"（version {n.version}, confidence={n.confidence}）"
        for n in neighbors
    )


class _Reconciler:
    """reconcile 的内部实现，持有共享组件避免逐条重建。"""

    def __init__(self, store, index, llm, embedder, settings):
        self.store: MarkdownStore = store
        self.index: IndexDB = index
        self.llm: LLMClient = llm
        self.embedder = embedder
        self.settings: Settings = settings
        self.searcher = HybridSearcher(store, index, embedder, settings)

    def find_neighbors(self, candidate: MemoryEntry) -> list[MemoryEntry]:
        """混合检索 top5，再用稠密距离阈值筛出真正的语义近邻。"""
        results = self.searcher.search(
            candidate.content, scopes=[candidate.scope], k=NEIGHBOR_TOP_K
        )
        if not results:
            return []
        vector = self.embedder.embed_texts([candidate.index_text])[0]
        distances = dict(
            self.index.search_dense(
                vector, k=_DISTANCE_LOOKUP_K, scopes=[candidate.scope, "global"]
            )
        )
        return [
            r.entry
            for r in results
            if distances.get(r.entry.id, 1.0) <= NEIGHBOR_MAX_DISTANCE
        ]

    def decide(self, candidate: MemoryEntry, neighbors: list[MemoryEntry]) -> dict:
        """调 LLM 判决策，返回规范化后的 decision dict。LLMError 向上抛。"""
        parsed = self.llm.complete_json(
            system=_SYSTEM_PROMPT,
            user=_USER_TEMPLATE.format(
                candidate_id=candidate.id,
                candidate_content=candidate.content,
                candidate_date=candidate.created_at.isoformat(),
                neighbors_text=_neighbors_text(neighbors),
            ),
            schema_description=_SCHEMA_DESCRIPTION,
        )
        action = str(parsed.get("action", "")).upper()
        if action not in ACTIONS:
            action = "CONFLICT"
        return {
            "action": action,
            "target_id": parsed.get("target_id") or None,
            "add_new": bool(parsed.get("add_new", False)),
            "reason": str(parsed.get("reason", "")),
        }

    # ---- 四种处置 ----

    def apply_add(self, candidate: MemoryEntry) -> str:
        self.store.create(candidate)
        self.index.upsert(candidate, self.embedder.embed_texts([candidate.index_text])[0])
        return candidate.id

    def apply_update(self, candidate: MemoryEntry, old: MemoryEntry) -> tuple[str, str]:
        """新条目 version 继承旧条目 +1、supersedes 指旧 id；旧条目删除。"""
        if candidate.id == old.id:
            # id 相同走 store.update（自动 version+1、刷新 last_verified）
            self.store.update(candidate.model_copy(update={"supersedes": None}))
            self.index.upsert(candidate, self.embedder.embed_texts([candidate.index_text])[0])
            return candidate.id, old.id
        new_entry = candidate.model_copy(
            update={"version": old.version + 1, "supersedes": old.id}
        )
        self.store.delete(old.id)
        self.index.delete(old.id)
        self.store.create(new_entry)
        self.index.upsert(new_entry, self.embedder.embed_texts([new_entry.index_text])[0])
        return new_entry.id, old.id

    def apply_delete(self, candidate: MemoryEntry, old: MemoryEntry, add_new: bool) -> None:
        self.store.delete(old.id)
        self.index.delete(old.id)
        if add_new:
            self.store.create(candidate)
            self.index.upsert(candidate, self.embedder.embed_texts([candidate.index_text])[0])

    def apply_noop(self, old: MemoryEntry) -> None:
        """重复信息：只刷新旧条目的 last_verified（store.update 会顺带 version+1）。"""
        updated = self.store.update(old)
        self.index.upsert(updated, self.embedder.embed_texts([updated.index_text])[0])

    def propagate(
        self, old: MemoryEntry, new: MemoryEntry | None, exclude_ids: set[str]
    ) -> PropagationReport:
        """UPDATE/DELETE 后的反向传播：检查依赖旧事实的既有条目是否失效。"""
        return propagate_change(
            self.store,
            self.index,
            self.embedder,
            self.settings,
            self.llm,
            old=old,
            new=new,
            exclude_ids=exclude_ids,
        )


def reconcile(
    candidates: list[MemoryEntry],
    store: MarkdownStore,
    index: IndexDB,
    llm: LLMClient,
    embedder=None,
    settings: Settings | None = None,
) -> ReconcileReport:
    """对账主流程：逐条候选找近邻 → 决策 → 落库 / 进复核队列。"""
    settings = settings or get_settings()
    embedder = embedder or get_embedder(settings)
    r = _Reconciler(store, index, llm, embedder, settings)
    report = ReconcileReport()

    for candidate in candidates:
        queue_reason: str | None = None
        neighbors = r.find_neighbors(candidate)
        if not neighbors:
            try:
                report.added.append(r.apply_add(candidate))
            except MemoryStoreError as e:
                report.queued.append((candidate, f"ADD 落库失败：{e}"))
            continue

        try:
            decision = r.decide(candidate, neighbors)
        except LLMError as e:
            # LLM 输出连续无法解析：不强行收敛，交人工复核（fail-safe）
            report.queued.append((candidate, f"LLM 决策不可用：{e}"))
            continue

        action = decision["action"]
        target_id = decision["target_id"]
        neighbor_ids = {n.id for n in neighbors}

        if action == "CONFLICT":
            queue_reason = (
                f"冲突无法自动收敛：{decision['reason'] or 'LLM 判定矛盾双方各有证据'}"
            )
        elif action == "ADD":
            try:
                report.added.append(r.apply_add(candidate))
            except MemoryStoreError as e:
                queue_reason = f"ADD 落库失败：{e}"
        elif action in {"UPDATE", "DELETE", "NOOP"}:
            if target_id not in neighbor_ids:
                queue_reason = (
                    f"LLM 决策的 target_id {target_id!r} 不在近邻集合内，拒绝执行"
                )
            else:
                old = store.get(target_id)
                if action == "UPDATE":
                    new_id, _old_id = r.apply_update(candidate, old)
                    report.updated.append((new_id, _old_id))
                    report.propagation.append(
                        r.propagate(old, new=store.get(new_id), exclude_ids={candidate.id})
                    )
                elif action == "DELETE":
                    r.apply_delete(candidate, old, decision["add_new"])
                    report.deleted.append(old.id)
                    if decision["add_new"]:
                        report.added.append(candidate.id)
                    new_entry = store.get(candidate.id) if decision["add_new"] else None
                    report.propagation.append(
                        r.propagate(old, new=new_entry, exclude_ids={candidate.id})
                    )
                else:
                    r.apply_noop(old)
                    report.noops.append(candidate.id)

        if queue_reason is not None:
            report.queued.append((candidate, queue_reason))

    if report.queued:
        report.queued_files = write_review_queue(
            [c for c, _ in report.queued],
            store.data_dir,
            reason="对账无法自动收敛",
        )
    # 传播产生的复核队列文件（需修订 / 判定失败的近邻）并入统一出口
    for p in report.propagation:
        report.queued_files.extend(p.queued_files)
    return report
