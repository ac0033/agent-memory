"""验证（五步循环第四步）：提案晋升前的三档检查，任一不过即整体否决。

三档分开判定、各自一票否决（不可平均分绕过）：
- boundary（LLM 判定）：该提案声称修复的问题场景是否被其变更真正覆盖——
  用提案自带的可证伪契约（证据/根因/预期修复）交 LLM 核查。变更分两类：
  自动应用类（merge/invalidate/downgrade/archive）核查契约达成；转人工类
  （conflict/revise）只核查标记是否准确、证据是否充分——"交人工裁决"是
  设计语义（apply 一律跳过），标记准确即视为该部分通过，不算契约未覆盖；
- retention（规则）：抽样既有记忆的关键检索场景在提案应用后不退化——
  把提案应用到临时目录的副本上，对一组基准 query 做 top-5 diff，
  提案未涉及的条目不得掉出 top5；
- safety（规则）：提案不得删除/弱化 safety 相关记忆（content 含 "安全"/"safety"
  的条目；schema 尚无 tags 字段，先用内容关键词判定，见 SAFETY_PATTERN）。
  conflict / revise 本来就是人工裁决项、不会被自动应用，不视为触碰。

boundary 的 LLM 判定失败（LLMError）按不通过处理（fail-closed）。
"""

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_memory.config import Settings
from agent_memory.llm import LLMClient, LLMError, ValidatingLLMClient
from agent_memory.long_term.evolve.apply import ApplyReport, _apply_changes
from agent_memory.long_term.retrieve.hybrid import HybridSearcher
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import EvolutionProposal

# safety 相关记忆的内容判定（schema 引入 tags 字段后改为判 tag）
SAFETY_PATTERN = re.compile(r"安全|safety", re.IGNORECASE)

# retention 档的基准 query top-k
_RETENTION_TOP_K = 5

# 会被自动应用的变更类型（conflict / revise 跳过，不参与 safety 拦截）
_AUTO_APPLY_KINDS = {"merge", "invalidate", "downgrade", "archive"}

_BOUNDARY_SCHEMA = """{
  "pass": true/false,
  "reason": "一句话依据"
}"""

_BOUNDARY_SYSTEM = (
    "你是记忆库整理提案的验证员。给你一份整理提案（每条变更带可证伪契约："
    "证据、根因、预期修复、可能受损面），你要判定该提案是否真正覆盖了它声称"
    "修复的问题场景。变更分两类，评估标准不同：\n"
    "\n"
    "- 自动应用类（merge / invalidate / downgrade / archive）：判定变更动作"
    "与根因是否匹配（如重复条目确实被 merge、失效条目确实被 invalidate）、"
    "预期修复是否可观察可证伪、有没有明显与契约自相矛盾之处；\n"
    "- 转人工类（conflict / revise）：这类变更的设计语义就是“机器不强行收敛、"
    "交人工裁决”，apply 阶段一律跳过、不会改动记忆层。对它你只评估标记是否"
    "准确、证据是否充分（如冲突双方确实对同一事实取值矛盾且各自有证据）；"
    "标记准确即视为该部分通过——“矛盾未在提案中解决”本身不是否决理由。\n"
    "\n"
    "拿不准时判不通过（宁可驳回让人工看，也不放过存疑提案）。"
)

_BOUNDARY_USER = """整理提案：{proposal_id}
整体可证伪契约：{contract}

变更清单：
{changes_text}

请按系统要求的 JSON 结构输出你的判定。"""


@dataclass
class TierResult:
    """单档验证结果。"""

    passed: bool
    reason: str


@dataclass
class VerifyReport:
    """三档验证结果；passed 是全过的合取（veto 语义）。"""

    boundary: TierResult
    retention: TierResult
    safety: TierResult

    @property
    def passed(self) -> bool:
        return self.boundary.passed and self.retention.passed and self.safety.passed

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "boundary": {"passed": self.boundary.passed, "reason": self.boundary.reason},
            "retention": {"passed": self.retention.passed, "reason": self.retention.reason},
            "safety": {"passed": self.safety.passed, "reason": self.safety.reason},
        }


def _check_safety(proposal: EvolutionProposal, store: MarkdownStore) -> TierResult:
    """safety 档：自动应用的变更不得触碰 safety 相关记忆。"""
    violations: list[str] = []
    for change in proposal.changes:
        if change.kind not in _AUTO_APPLY_KINDS:
            continue
        for tid in change.target_ids:
            try:
                entry = store.get(tid)
            except KeyError:
                continue
            if SAFETY_PATTERN.search(entry.content) or (
                entry.detail and SAFETY_PATTERN.search(entry.detail)
            ):
                violations.append(f"{change.kind} -> {tid}")
    if violations:
        return TierResult(
            False, "提案试图删除/弱化 safety 相关记忆：" + ", ".join(violations)
        )
    return TierResult(True, "未触碰 safety 相关记忆")


def _check_boundary(proposal: EvolutionProposal, llm: LLMClient) -> TierResult:
    """boundary 档：LLM 核查提案是否覆盖其契约声称修复的问题场景。"""
    changes_text = "\n".join(
        f"- [{c.kind}]（{'自动应用' if c.kind in _AUTO_APPLY_KINDS else '转人工，apply 跳过'}）"
        f" targets={','.join(c.target_ids)}\n"
        f"  依据: {c.reason}\n"
        f"  契约: 证据={c.contract.evidence}；根因={c.contract.root_cause}；"
        f"预期修复={c.contract.expected_fix}；受损面={c.contract.blast_radius}"
        for c in proposal.changes
    ) or "（无变更）"
    try:
        parsed = llm.complete_json(
            system=_BOUNDARY_SYSTEM,
            user=_BOUNDARY_USER.format(
                proposal_id=proposal.id,
                contract=proposal.falsifiable_contract,
                changes_text=changes_text,
            ),
            schema_description=_BOUNDARY_SCHEMA,
        )
    except LLMError as e:
        return TierResult(False, f"boundary 判定不可用（fail-closed 按不通过）：{e}")
    passed = bool(parsed.get("pass", False))
    return TierResult(passed, str(parsed.get("reason", "")) or ("通过" if passed else "未通过"))


def _benchmark_queries(
    proposal: EvolutionProposal, store: MarkdownStore, settings: Settings
) -> list[str]:
    """retention 档的基准 query：提案未涉及的既有记忆 content（按 id 排序取前 N 条）。"""
    targeted = {tid for c in proposal.changes for tid in c.target_ids}
    candidates = [e for e in store.list(scope=proposal.scope) if e.id not in targeted]
    return [e.content for e in candidates[: settings.evolve_retention_query_count]]


def _check_retention(
    proposal: EvolutionProposal,
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    settings: Settings,
) -> TierResult:
    """retention 档：提案应用到临时副本后，基准 query 的 top5 不得丢失无关条目。"""
    queries = _benchmark_queries(proposal, store, settings)
    if not queries:
        return TierResult(True, "提案涉及全部条目，无基准 query 可抽查，默认通过")
    searcher = HybridSearcher(store, index, embedder, settings)
    before = {q: {r.entry.id for r in searcher.search(q, k=_RETENTION_TOP_K)} for q in queries}

    with tempfile.TemporaryDirectory() as tmp:
        scratch_dir = Path(tmp)
        memory_src = store.memory_dir
        scratch_store = MarkdownStore(scratch_dir)
        if memory_src.exists():
            shutil.copytree(memory_src, scratch_store.memory_dir)
        scratch_index = IndexDB(scratch_dir / "index.db")
        try:
            for entry in scratch_store.list():
                scratch_index.upsert(entry, embedder.embed_texts([entry.index_text])[0])
            _apply_changes(proposal.changes, scratch_store, scratch_index, embedder, ApplyReport())
            scratch_searcher = HybridSearcher(scratch_store, scratch_index, embedder, settings)
            after = {
                q: {r.entry.id for r in scratch_searcher.search(q, k=_RETENTION_TOP_K)}
                for q in queries
            }
        finally:
            scratch_index.close()

    targeted = {tid for c in proposal.changes for tid in c.target_ids}
    dropped: list[str] = []
    for q in queries:
        lost = before[q] - after[q] - targeted
        if lost:
            dropped.append(f"query {q!r} 丢失 {sorted(lost)}")
    if dropped:
        return TierResult(False, "提案应用后既有检索场景退化：" + "；".join(dropped))
    return TierResult(True, f"{len(queries)} 条基准 query 的 top5 无无关条目丢失")


def verify_proposal(
    proposal: EvolutionProposal,
    store: MarkdownStore,
    index: IndexDB,
    embedder,
    settings: Settings,
    llm: LLMClient,
) -> VerifyReport:
    """三档验证主流程。三档独立判定，任一不过则整体不晋升。"""
    if not isinstance(llm, ValidatingLLMClient):
        llm = ValidatingLLMClient(llm)
    safety = _check_safety(proposal, store)
    boundary = _check_boundary(proposal, llm)
    retention = _check_retention(proposal, store, index, embedder, settings)
    return VerifyReport(boundary=boundary, retention=retention, safety=safety)
