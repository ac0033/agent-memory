"""整合（五步循环第三步的产出环节）：扫描记忆库，产出 EvolutionProposal。

四类整理动作：
1. 去重合并：稠密距离 ≤ evolve_merge_max_distance 的同 scope 条目对交 LLM 判
   MERGE / CONFLICT / UNRELATED；MERGE 产出合并条目（content 自包含、evidence
   并集、version = 两者最大 +1、supersedes 指主条目），CONFLICT 不强行收敛，
   标记进提案交人工裁决；
2. 离线抽查复核：按 last_verified 最旧抽查（evolve_stale_sample_size 条），
   用 ingest/propagate.py 的 judge_validity 判定该条目本身是否仍成立
   （默认假设成立，明确证据才判失效，证据不足降级为 revise 交人工——
   不复用 judge_propagation：它的语义是"某条记忆被撤销后牵连谁"，拿来
   复核条目自身会把正常记忆恒判失效，见 docs/m2-defect-postmortem.md）；
3. 长期未被检索（retrieval_count == 0 且创建超过 stale_days 天）的条目：
   confidence 非 low 的建议降一档（downgrade），已是 low 的建议归档（archive）；
4. 反"丢弃式防御"（postmortem 启示 5）：LLM 合并产出先做规范化
   （normalize_entry_id）再校验，仍非法时不丢弃，降级为 revise 变更交人工。

硬边界：本模块只产出提案对象，save_proposal 把它写到
data/review_queue/evolution/<timestamp>/，绝不直接改 data/memory。
"""

import hashlib
from datetime import datetime
from pathlib import Path

import yaml

from agent_memory.config import Settings
from agent_memory.ingest.propagate import judge_validity
from agent_memory.llm import LLMClient, LLMError
from agent_memory.models import (
    Confidence,
    EvolutionChange,
    EvolutionProposal,
    FalsifiableContract,
    MemoryEntry,
    normalize_entry_id,
)
from agent_memory.store.index_db import IndexDB
from agent_memory.store.markdown_store import MarkdownStore

# 合并判定的近邻检索条数（同 scope 内找重复对）
_MERGE_NEIGHBOR_K = 10

_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}

_MERGE_SCHEMA = """{
  "verdict": "MERGE | CONFLICT | UNRELATED",
  "merged_id": "合并后条目 id（kebab-case，仅 MERGE 必填，否则 null）",
  "merged_content": "合并后的自包含原子句（仅 MERGE 必填，否则 null）",
  "merged_detail": "可选：带前因后果的完整段落，null 表示无",
  "reason": "一句话依据"
}"""

_MERGE_SYSTEM = (
    "你是记忆库整理员。给你两条语义相近的既有记忆，判定它们的关系：\n"
    "\n"
    "- MERGE：两条说的是同一事实、互为重复，应合并为一条。合并后的 content 必须"
    "自包含（不看原文也能懂）、不丢任何一条独有的信息；\n"
    "- CONFLICT：两条对同一事实的取值相互矛盾，且各自都有证据，你无法判断哪个"
    "成立——不强行收敛，交人工裁决。主题相近但讲不同侧面（互为补充）不是冲突；\n"
    "- UNRELATED：只是主题相近，各有各的信息，都应保留。\n"
    "\n"
    "判断依据只看事实关系，不要被措辞差异迷惑。"
)

_MERGE_USER = """既有记忆 A：
- id: {a_id}
- 内容: {a_content}
- 证据数: {a_evidence}，最后核实: {a_verified}

既有记忆 B：
- id: {b_id}
- 内容: {b_content}
- 证据数: {b_evidence}，最后核实: {b_verified}

请按系统要求的 JSON 结构输出你的判定。"""


def _proposal_id(now: datetime, changes: list[EvolutionChange]) -> str:
    """evolve-<yyyymmdd>-<hash6>：同刻同内容同 id，重复生成覆盖同一提案目录。"""
    payload = "|".join(f"{c.kind}:{','.join(c.target_ids)}" for c in changes)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:6]
    return f"evolve-{now:%Y%m%d}-{digest}"


def _contract(
    evidence: str, root_cause: str, expected_fix: str, blast_radius: str
) -> FalsifiableContract:
    return FalsifiableContract(
        evidence=evidence, root_cause=root_cause,
        expected_fix=expected_fix, blast_radius=blast_radius,
    )


class _Consolidator:
    """consolidate 的内部实现，持有共享组件。"""

    def __init__(self, store, index, embedder, settings, llm, scope, now):
        self.store: MarkdownStore = store
        self.index: IndexDB = index
        self.embedder = embedder
        self.settings: Settings = settings
        self.llm: LLMClient = llm
        self.scope: str | None = scope
        self.now: datetime = now
        self.changes: list[EvolutionChange] = []
        self.consumed: set[str] = set()  # 已被 merge/conflict 占用的条目 id

    # ---- 去重合并 ----

    def find_duplicate_pairs(
        self, entries: list[MemoryEntry]
    ) -> list[tuple[MemoryEntry, MemoryEntry]]:
        """同 scope 内稠密距离 ≤ 阈值的条目对（无序去重）。"""
        pairs: dict[tuple[str, str], tuple[MemoryEntry, MemoryEntry]] = {}
        by_id = {e.id: e for e in entries}
        for entry in entries:
            vector = self.embedder.embed_texts([entry.index_text])[0]
            for nid, distance in self.index.search_dense(
                vector, k=_MERGE_NEIGHBOR_K, scopes=[entry.scope]
            ):
                if nid == entry.id or nid not in by_id:
                    continue
                if distance > self.settings.evolve_merge_max_distance:
                    continue
                key = tuple(sorted((entry.id, nid)))
                pairs.setdefault(key, (by_id[key[0]], by_id[key[1]]))
        return list(pairs.values())

    def judge_pair(self, a: MemoryEntry, b: MemoryEntry) -> dict:
        parsed = self.llm.complete_json(
            system=_MERGE_SYSTEM,
            user=_MERGE_USER.format(
                a_id=a.id, a_content=a.content,
                a_evidence=len(a.evidence), a_verified=a.last_verified.isoformat(),
                b_id=b.id, b_content=b.content,
                b_evidence=len(b.evidence), b_verified=b.last_verified.isoformat(),
            ),
            schema_description=_MERGE_SCHEMA,
        )
        verdict = str(parsed.get("verdict", "")).upper()
        if verdict not in {"MERGE", "CONFLICT", "UNRELATED"}:
            verdict = "UNRELATED"
        return {
            "verdict": verdict,
            "merged_id": parsed.get("merged_id") or None,
            "merged_content": parsed.get("merged_content") or None,
            "merged_detail": parsed.get("merged_detail") or None,
            "reason": str(parsed.get("reason", "")),
        }

    def build_merged_entry(self, a: MemoryEntry, b: MemoryEntry, decision: dict) -> MemoryEntry:
        """构造合并条目：规范化优先于校验（normalize_entry_id），调用方捕获 ValueError。"""
        raw_id = decision["merged_id"] or a.id
        merged_id = normalize_entry_id(raw_id) or a.id
        confidence: Confidence = min(
            (a.confidence, b.confidence), key=lambda c: _CONFIDENCE_ORDER[c]
        )
        evidence = list(a.evidence)
        seen = {(e.session_id, e.source, e.line_range) for e in evidence}
        for e in b.evidence:
            if (e.session_id, e.source, e.line_range) not in seen:
                evidence.append(e)
        return MemoryEntry(
            id=merged_id,
            content=decision["merged_content"] or "",
            detail=decision["merged_detail"] or None,
            memory_type=a.memory_type,
            scope=a.scope,
            confidence=confidence,
            source=f"{a.source}+{b.source}",
            evidence=evidence,
            created_at=min(a.created_at, b.created_at),
            last_verified=self.now.date(),
            version=max(a.version, b.version) + 1,
            supersedes=a.id,
            retrieval_count=a.retrieval_count + b.retrieval_count,
        )

    def collect_merges(self, entries: list[MemoryEntry]) -> None:
        for a, b in self.find_duplicate_pairs(entries):
            if a.id in self.consumed or b.id in self.consumed:
                continue  # 一条只参与一次合并/冲突判定，避免提案自相矛盾
            try:
                decision = self.judge_pair(a, b)
            except LLMError as e:
                # 判定失败不动作、降级为 revise 交人工（fail-safe，与 propagate 一致）
                self.changes.append(EvolutionChange(
                    kind="revise", target_ids=[a.id, b.id],
                    reason=f"合并判定不可用：{e}",
                    contract=_contract(
                        evidence=f"稠密距离 ≤ {self.settings.evolve_merge_max_distance} 的近似对",
                        root_cause="LLM 合并判定连续失败，机器无法决策",
                        expected_fix="人工查看两条记忆是否重复或冲突",
                        blast_radius="不应用则记忆层保持现状，无影响",
                    ),
                ))
                self.consumed.update((a.id, b.id))
                continue
            if decision["verdict"] == "UNRELATED":
                continue
            if decision["verdict"] == "CONFLICT":
                self.changes.append(EvolutionChange(
                    kind="conflict", target_ids=[a.id, b.id],
                    reason=decision["reason"] or "语义矛盾且各有证据，交人工裁决",
                    contract=_contract(
                        evidence=f"两条记忆对同一事实取值矛盾：{a.id} / {b.id}",
                        root_cause="不同时期沉淀的事实发生冲突，机器无法凭现有信息收敛",
                        expected_fix="人工确认哪个取值成立，删除或改写另一条",
                        blast_radius="裁决前两条都保留，召回可能给出矛盾答案",
                    ),
                ))
                self.consumed.update((a.id, b.id))
                continue
            # MERGE：规范化后仍非法时不丢弃，降级为 revise（postmortem 启示 5）
            try:
                merged = self.build_merged_entry(a, b, decision)
            except ValueError as e:
                self.changes.append(EvolutionChange(
                    kind="revise", target_ids=[a.id, b.id],
                    reason=f"LLM 合并产出非法，无法自动入库：{e}",
                    contract=_contract(
                        evidence=f"判 MERGE 但产出过不了 schema 校验：{e}",
                        root_cause="LLM 合并产出不合法（空 content / 超长等）",
                        expected_fix="人工按合并意图改写后入库",
                        blast_radius="不应用则两条原始记忆保持现状，无影响",
                    ),
                ))
            else:
                self.changes.append(EvolutionChange(
                    kind="merge", target_ids=[a.id, b.id], merged_entry=merged,
                    reason=decision["reason"] or "语义重复，合并为一条",
                    contract=_contract(
                        evidence=(
                            f"稠密距离 ≤ {self.settings.evolve_merge_max_distance}，"
                            f"LLM 判 MERGE；合并 evidence {len(merged.evidence)} 条"
                        ),
                        root_cause="同一事实在不同时期被重复沉淀为两条",
                        expected_fix=f"合并为 {merged.id}（version={merged.version}），原两条移除",
                        blast_radius=f"以 {a.id} / {b.id} 为目标的检索改由 {merged.id} 承接",
                    ),
                ))
            self.consumed.update((a.id, b.id))

    # ---- 离线抽查复核（抽查最旧条目，判定其本身是否仍成立）----

    def _validity_neighbors(
        self, entry: MemoryEntry, entries: list[MemoryEntry]
    ) -> list[MemoryEntry]:
        """取与待复核条目语义最相近的既有条目，供 judge_validity 对照
        （是否被更新条目取代 / 是否与现存条目矛盾）。"""
        by_id = {e.id: e for e in entries}
        vector = self.embedder.embed_texts([entry.index_text])[0]
        neighbors: list[MemoryEntry] = []
        for nid, _distance in self.index.search_dense(
            vector, k=_MERGE_NEIGHBOR_K, scopes=[entry.scope, "global"]
        ):
            if nid == entry.id or nid not in by_id:
                continue
            neighbors.append(by_id[nid])
        return neighbors

    def collect_stale_recheck(self, entries: list[MemoryEntry]) -> None:
        oldest = sorted(
            (e for e in entries if e.id not in self.consumed),
            key=lambda e: e.last_verified,
        )[: self.settings.evolve_stale_sample_size]
        for entry in oldest:
            try:
                # 复核条目自身是否仍成立（不是传播判定，语义见 judge_validity）
                verdict = judge_validity(
                    self.llm, entry, self._validity_neighbors(entry, entries)
                )
            except LLMError as e:
                self.changes.append(EvolutionChange(
                    kind="revise", target_ids=[entry.id],
                    reason=f"离线复核判定不可用：{e}",
                    contract=_contract(
                        evidence=f"last_verified={entry.last_verified}，为全库最旧的一批",
                        root_cause="LLM 复核判定连续失败",
                        expected_fix="人工核查该记忆是否仍成立",
                        blast_radius="不应用则记忆层保持现状，无影响",
                    ),
                ))
                self.consumed.add(entry.id)
                continue
            if verdict["verdict"] == "INVALIDATED":
                self.changes.append(EvolutionChange(
                    kind="invalidate", target_ids=[entry.id],
                    reason=verdict["reason"] or "离线复核判定已不再成立",
                    contract=_contract(
                        evidence=f"last_verified={entry.last_verified}；LLM 复核判 INVALIDATED",
                        root_cause="事实随时间失效，在线路径没有触发过对它的更新",
                        expected_fix="从记忆层移除（审计日志留完整快照，可回滚）",
                        blast_radius=f"以 {entry.id} 为命中的检索不再返回该条",
                    ),
                ))
                self.consumed.add(entry.id)
            elif verdict["verdict"] == "NEEDS_REVISION":
                self.changes.append(EvolutionChange(
                    kind="revise", target_ids=[entry.id],
                    reason=verdict["reason"] or "离线复核判定需修订，交人工",
                    contract=_contract(
                        evidence=f"last_verified={entry.last_verified}；LLM 复核判 NEEDS_REVISION",
                        root_cause="记忆部分失效但机器证据不足以改写",
                        expected_fix="人工修订措辞或补充例外",
                        blast_radius="不应用则记忆层保持现状，无影响",
                    ),
                ))
                self.consumed.add(entry.id)

    # ---- 长期未检索条目：降权 / 归档建议 ----

    def collect_cold_entries(self, entries: list[MemoryEntry]) -> None:
        today = self.now.date()
        for entry in entries:
            if entry.id in self.consumed:
                continue
            if entry.retrieval_count > 0:
                continue
            if (today - entry.created_at).days <= self.settings.stale_days:
                continue
            evidence = (
                f"retrieval_count=0，创建于 {entry.created_at}"
                f"（已超 stale_days={self.settings.stale_days} 天未被检索）"
            )
            if entry.confidence != "low":
                new_confidence: Confidence = (
                    "medium" if entry.confidence == "high" else "low"
                )
                self.changes.append(EvolutionChange(
                    kind="downgrade", target_ids=[entry.id], new_confidence=new_confidence,
                    reason="长期未被检索，置信度降一档",
                    contract=_contract(
                        evidence=evidence,
                        root_cause="记忆沉淀后从未被任何检索命中，实用价值存疑",
                        expected_fix=f"confidence 降为 {new_confidence}，排序权重下调",
                        blast_radius="该条在混合检索中的综合分下降，边缘场景可能不再进 top-k",
                    ),
                ))
            else:
                self.changes.append(EvolutionChange(
                    kind="archive", target_ids=[entry.id],
                    reason="长期未被检索且已是 low 置信度，建议归档",
                    contract=_contract(
                        evidence=evidence + "，confidence=low",
                        root_cause="低置信度且从未被检索，保留只会稀释召回质量",
                        expected_fix="移出活跃记忆层（应用前快照，可回滚）",
                        blast_radius=f"以 {entry.id} 为命中的检索不再返回该条",
                    ),
                ))


def build_proposal(
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    settings: Settings,
    llm: LLMClient,
    scope: str | None = None,
    now: datetime | None = None,
) -> EvolutionProposal:
    """扫描指定 scope（None = 全库），产出整理提案。不改记忆层。"""
    now = now or datetime.now()
    entries = store.list(scope=scope)
    c = _Consolidator(store, index, embedder, settings, llm, scope, now)
    c.collect_merges(entries)
    c.collect_stale_recheck(entries)
    c.collect_cold_entries(entries)
    return EvolutionProposal(
        id=_proposal_id(now, c.changes),
        scope=scope,
        changes=c.changes,
        falsifiable_contract=(
            f"本提案含 {len(c.changes)} 条变更（merge/conflict/invalidate/revise/"
            f"downgrade/archive）。若应用后既有检索场景的 top5 丢失无关条目、"
            f"safety 相关记忆被删除或弱化、或变更声称修复的问题场景仍复现，"
            f"则该提案应被驳回。"
        ),
        created_by="evolve-cycle",
        created_at=now,
    )


def save_proposal(proposal: EvolutionProposal, data_dir: Path) -> Path:
    """把提案写到 data/review_queue/evolution/<timestamp>/proposal.yaml。"""
    proposal_dir = (
        Path(data_dir) / "review_queue" / "evolution" / f"{proposal.created_at:%Y%m%dT%H%M%S}"
    )
    proposal_dir.mkdir(parents=True, exist_ok=True)
    (proposal_dir / "proposal.yaml").write_text(
        yaml.safe_dump(proposal.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return proposal_dir
