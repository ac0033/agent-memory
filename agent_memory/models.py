"""记忆条目 schema（M0 核心）。

三层数据模型中的"知识层"定义：
- data/raw 里的原始会话是证据层，MemoryEntry 通过 evidence 指针回溯到它；
- data/memory 里的 Markdown 是唯一事实来源，每条记忆就是一个 MemoryEntry；
- index.db 是可重建的派生索引，不在此处建模。
"""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MemoryType = Literal["semantic", "procedural", "episodic", "profile"]
Confidence = Literal["high", "medium", "low"]

# scope 取值空间：全局 / 某个仓库 / 某个 agent
_SCOPE_RE = re.compile(r"^(global|repo:[a-z0-9-]+|agent:[a-z0-9-]+)$")
# kebab-case：小写字母数字，单词间单个连字符
_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# 规范化用：非 [a-z0-9-] 的字符段一律折叠成一个连字符
_KEBAB_INVALID_RUN_RE = re.compile(r"[^a-z0-9-]+")
_KEBAB_MULTI_DASH_RE = re.compile(r"-{2,}")

CONTENT_MAX_CHARS = 500
DETAIL_MAX_CHARS = 800


def normalize_entry_id(raw_id: str) -> str:
    """把任意字符串规范化为 kebab-case id。

    规则：先转小写，再把非 [a-z0-9-] 的字符段（点号、下划线、空格、中文等）
    一律替换为单个连字符，折叠重复连字符、去首尾连字符。
    例：python-version-upgrade-to-3.12 -> python-version-upgrade-to-3-12。
    返回值可能仍是非法 id（如全由无效字符组成时得到空串），调用方负责兜底。
    """
    slug = _KEBAB_INVALID_RUN_RE.sub("-", raw_id.strip().lower())
    return _KEBAB_MULTI_DASH_RE.sub("-", slug).strip("-")


def normalize_scope(raw: str) -> str:
    """把 scope 归一化为合法形式：去空白、转小写，slug 部分的非法字符段
    （下划线、点号、空格等）折叠为单个连字符。

    例：repo:llm_wiki -> repo:llm-wiki、GLOBAL -> global。
    无法归一为合法 scope 的输入（未知前缀、空 slug 等）仅小写化后原样返回，
    由 is_valid_scope / MemoryEntry 校验 fail-closed。
    """
    s = raw.strip().lower()
    if s == "global":
        return s
    prefix, sep, slug = s.partition(":")
    if sep and prefix in ("repo", "agent") and slug:
        return f"{prefix}:{normalize_entry_id(slug)}"
    return s


def is_valid_scope(v: str) -> bool:
    """scope 是否符合 global | repo:<slug> | agent:<name>（slug 为 kebab-case）。"""
    return bool(_SCOPE_RE.fullmatch(v))


def is_valid_entry_id(v: str) -> bool:
    """entry id 是否为严格 kebab-case。"""
    return bool(_KEBAB_RE.fullmatch(v))


def validate_entry_id(v: str) -> str:
    """校验外部传入的 entry id，防 glob/路径语义进入文件操作。"""
    if not is_valid_entry_id(v):
        raise ValueError(f"id 必须是 kebab-case（小写字母/数字/连字符），收到: {v!r}")
    return v


class EvidenceRef(BaseModel):
    """指向 raw 层证据的指针。"""

    session_id: str
    source: str  # 哪个 agent / 哪次会话
    line_range: tuple[int, int] | None = None


class MemoryEntry(BaseModel):
    """一条原子记忆：一句话事实 + 来源 + 证据指针 + 版本化字段。

    content 是原子句（Simple Notes）；detail 是可选的完整段落
    （Enhanced Notes：带前因后果的 2-4 句话）。两者混合的取舍：
    content 负责注入渲染的简洁，detail 负责保留叙事完整性、
    并拼进索引文本（向量 + FTS）提高召回率，代价是存储冗余。
    detail 为可选字段，老数据（无 detail 的记忆文件）兼容读取。
    """

    id: str  # kebab-case slug
    content: str  # 原子事实，一句话
    detail: str | None = None  # 可选：带完整上下文的段落（≤ DETAIL_MAX_CHARS）
    memory_type: MemoryType
    scope: str  # "global" / "repo:<slug>" / "agent:<name>"
    confidence: Confidence
    source: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    created_at: date
    last_verified: date
    version: int = 1
    supersedes: str | None = None  # 版本化冲突：本条目取代的旧条目 id
    # 检索命中计数（M4a）：hybrid 检索返回该条目时 +1（写路径的近邻检索不计）。
    # 长期为 0 的条目会在整理循环里被建议降权/归档。旧记忆文件无此字段，默认 0 兼容读取。
    retrieval_count: int = Field(default=0, ge=0)

    @field_validator("id")
    @classmethod
    def id_must_be_kebab_case(cls, v: str) -> str:
        return validate_entry_id(v)

    @field_validator("scope")
    @classmethod
    def scope_must_match_pattern(cls, v: str) -> str:
        if not is_valid_scope(v):
            raise ValueError(
                "scope 必须匹配 global | repo:<slug> | agent:<name>，"
                f"slug 为小写字母/数字/连字符，收到: {v!r}"
            )
        return v

    @field_validator("content")
    @classmethod
    def content_must_be_atomic(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("content 不能为空")
        if len(stripped) > CONTENT_MAX_CHARS:
            raise ValueError(
                f"content 长度 {len(stripped)} 超过上限 {CONTENT_MAX_CHARS} 字符，"
                "记忆应保持原子化（一句话一个事实）"
            )
        return stripped

    @field_validator("detail")
    @classmethod
    def detail_must_be_bounded(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None  # 空白 detail 等价于没有 detail
        if len(stripped) > DETAIL_MAX_CHARS:
            raise ValueError(
                f"detail 长度 {len(stripped)} 超过上限 {DETAIL_MAX_CHARS} 字符，"
                "detail 是带上下文的完整段落（2-4 句话），不是全文转录"
            )
        return stripped

    @property
    def index_text(self) -> str:
        """索引（向量嵌入 + FTS 全文）用文本：detail 存在时拼接在 content 后。"""
        if self.detail:
            return f"{self.content}\n{self.detail}"
        return self.content

    @model_validator(mode="after")
    def profile_memory_cannot_be_low_confidence(self) -> "MemoryEntry":
        # profile（用户画像）是长期身份性事实，低置信度的画像没有沉淀价值，直接拒绝
        if self.memory_type == "profile" and self.confidence == "low":
            raise ValueError("memory_type 为 profile 时 confidence 不允许为 low")
        return self


class MemoryProposal(BaseModel):
    """蒸馏产出的记忆提案（M2/M4 用，M0 只定义 schema）。

    提案不直接入库：必须带可证伪契约（falsifiable_contract），
    经对账门（reconcile）确认后才落进 data/memory。
    """

    candidates: list[MemoryEntry]
    falsifiable_contract: str  # 可证伪契约文本：写明什么证据出现时该提案应被驳回
    created_by: str  # 提案者标识（哪个蒸馏模型 / 哪次运行）

    @field_validator("falsifiable_contract", "created_by")
    @classmethod
    def must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v


# ---------------------------------------------------------------- M4a 整理提案

# 整理变更类型：
# - merge：两条语义重复条目合并为一条（被合并者从记忆层移除，merged_entry 入库）；
# - conflict：语义矛盾但各有证据，不自动收敛，交人工裁决（apply 跳过）；
# - invalidate：离线复核判定已不再成立，建议删除（审计日志留完整快照）；
# - revise：离线复核判定需要修订但机器证据不足，交人工裁决（apply 跳过）；
# - downgrade：长期未被检索，confidence 降一档；
# - archive：长期未被检索且已是 low，建议移出活跃记忆层（删除，快照可回滚）。
EvolutionChangeKind = Literal[
    "merge", "conflict", "invalidate", "revise", "downgrade", "archive"
]


class FalsifiableContract(BaseModel):
    """可证伪契约：一条变更凭什么成立、怎么验证、可能误伤谁。"""

    evidence: str  # 证据：支撑该变更的观察（相似度、last_verified、retrieval_count 等）
    root_cause: str  # 根因：问题为什么会发生
    expected_fix: str  # 预期修复：应用后应观察到什么
    blast_radius: str  # 可能受损面：哪些检索场景/条目可能受影响

    @field_validator("evidence", "root_cause", "expected_fix", "blast_radius")
    @classmethod
    def contract_fields_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("可证伪契约字段不能为空")
        return v


class EvolutionChange(BaseModel):
    """整理提案中的一条变更。conflict / revise 是人工裁决项，apply 一律跳过。"""

    kind: EvolutionChangeKind
    target_ids: list[str] = Field(min_length=1)  # 涉及的既有条目 id
    merged_entry: MemoryEntry | None = None  # 仅 merge：合并产出的新条目
    new_confidence: Confidence | None = None  # 仅 downgrade：降到的置信度
    reason: str  # 一句话依据（LLM 判定理由或规则说明）
    contract: FalsifiableContract

    @model_validator(mode="after")
    def kind_payload_consistent(self) -> "EvolutionChange":
        if self.kind == "merge" and self.merged_entry is None:
            raise ValueError("merge 变更必须携带 merged_entry")
        if self.kind == "downgrade" and self.new_confidence is None:
            raise ValueError("downgrade 变更必须携带 new_confidence")
        return self


class EvolutionProposal(BaseModel):
    """整理循环产出的提案（扩展 MemoryProposal 的契约思想）。

    硬边界：提案只写 data/review_queue/evolution/<timestamp>/，绝不直接改记忆层；
    必须过 verify 的三档验证（boundary / retention / safety，任一不过即否决）
    才允许 apply 晋升。
    """

    id: str  # kebab-case，如 evolve-20260820-ab12cd
    scope: str | None = None  # 本次整理的 scope，None 表示全库
    changes: list[EvolutionChange] = Field(default_factory=list)
    falsifiable_contract: str  # 整体契约：本批变更声称解决什么、如何证伪
    created_by: str  # 提案者标识
    created_at: datetime

    @field_validator("id")
    @classmethod
    def id_must_be_kebab_case(cls, v: str) -> str:
        if not _KEBAB_RE.fullmatch(v):
            raise ValueError(f"id 必须是 kebab-case，收到: {v!r}")
        return v

    @field_validator("falsifiable_contract", "created_by")
    @classmethod
    def must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v
