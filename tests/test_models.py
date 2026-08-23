"""MemoryEntry / EvidenceRef / MemoryProposal / EvolutionProposal 的校验规则测试。

覆盖 M0 schema 的全部校验规则：正常路径 + 异常路径；M4a 新增 retrieval_count
与整理提案（EvolutionChange / EvolutionProposal / FalsifiableContract）。
"""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from agent_memory.models import (
    CONTENT_MAX_CHARS,
    DETAIL_MAX_CHARS,
    EvidenceRef,
    EvolutionChange,
    EvolutionProposal,
    FalsifiableContract,
    MemoryEntry,
    MemoryProposal,
    is_valid_scope,
    normalize_scope,
)

TODAY = date(2026, 8, 19)


def make_entry(**overrides) -> dict:
    """构造一条合法 MemoryEntry 的字段，按需覆盖。"""
    fields = {
        "id": "user-prefers-uv",
        "content": "用户使用 uv 管理 Python 环境。",
        "memory_type": "semantic",
        "scope": "global",
        "confidence": "high",
        "source": "langgraph-session",
        "evidence": [EvidenceRef(session_id="s1", source="langgraph", line_range=(3, 5))],
        "created_at": TODAY,
        "last_verified": TODAY,
    }
    fields.update(overrides)
    return fields


class TestMemoryEntryHappyPath:
    def test_minimal_valid_entry(self):
        entry = MemoryEntry(**make_entry())
        assert entry.version == 1
        assert entry.supersedes is None
        assert entry.memory_type == "semantic"

    @pytest.mark.parametrize("scope", ["global", "repo:agent-memory", "agent:kimi-cli", "repo:a1"])
    def test_valid_scopes(self, scope):
        entry = MemoryEntry(**make_entry(scope=scope))
        assert entry.scope == scope

    def test_all_memory_types_accepted(self):
        for mt in ["semantic", "procedural", "episodic", "profile"]:
            entry = MemoryEntry(**make_entry(memory_type=mt, confidence="high"))
            assert entry.memory_type == mt

    def test_content_is_stripped(self):
        entry = MemoryEntry(**make_entry(content="  带首尾空白的内容。  "))
        assert entry.content == "带首尾空白的内容。"

    def test_content_at_max_length_ok(self):
        entry = MemoryEntry(**make_entry(content="x" * CONTENT_MAX_CHARS))
        assert len(entry.content) == CONTENT_MAX_CHARS

    def test_evidence_line_range_optional(self):
        entry = MemoryEntry(
            **make_entry(evidence=[EvidenceRef(session_id="s1", source="kimi-cli")])
        )
        assert entry.evidence[0].line_range is None

    def test_versioning_fields(self):
        entry = MemoryEntry(**make_entry(version=2, supersedes="user-prefers-pip"))
        assert entry.version == 2
        assert entry.supersedes == "user-prefers-pip"


class TestIdValidation:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "User-Prefers-Uv", "user_prefers_uv", "user prefers uv",
            "-leading", "trailing-", "a--b", "",
        ],
    )
    def test_non_kebab_case_rejected(self, bad_id):
        with pytest.raises(ValidationError, match="kebab-case"):
            MemoryEntry(**make_entry(id=bad_id))

    @pytest.mark.parametrize("good_id", ["a", "abc", "a-b-c", "abc123", "a1-b2"])
    def test_kebab_case_accepted(self, good_id):
        assert MemoryEntry(**make_entry(id=good_id)).id == good_id


class TestScopeValidation:
    @pytest.mark.parametrize(
        "bad_scope",
        [
            "GLOBAL",
            "repo:",          # slug 为空
            "agent:Kimi",     # slug 含大写
            "repo:a_b",       # slug 含下划线
            "user:me",        # 未知前缀
            "repo :x",        # 含空格
            "",
        ],
    )
    def test_invalid_scopes_rejected(self, bad_scope):
        with pytest.raises(ValidationError, match="scope"):
            MemoryEntry(**make_entry(scope=bad_scope))


class TestNormalizeScope:
    """服务端入口的 scope 归一化：可修复的旧写法折叠为合法形式，垃圾输入保持非法。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("repo:llm_wiki", "repo:llm-wiki"),   # 下划线折叠
            ("GLOBAL", "global"),                  # 大小写
            ("Repo:My_Proj.X", "repo:my-proj-x"),  # 前缀大小写 + 混合非法字符
            ("agent:kimi code", "agent:kimi-code"),  # 空格折叠
            ("  repo:a-b  ", "repo:a-b"),          # 首尾空白
            ("global", "global"),                  # 合法输入原样
            ("repo:a-b", "repo:a-b"),
        ],
    )
    def test_normalizable_scopes(self, raw, expected):
        assert normalize_scope(raw) == expected
        assert is_valid_scope(normalize_scope(raw))

    @pytest.mark.parametrize(
        "bad_scope",
        [
            "not a scope",   # 无前缀
            "user:me",       # 未知前缀
            "repo:",         # 空 slug
            "",              # 空串
        ],
    )
    def test_unfixable_scopes_stay_invalid(self, bad_scope):
        assert not is_valid_scope(normalize_scope(bad_scope))


class TestContentValidation:
    @pytest.mark.parametrize("bad_content", ["", "   ", "\n\t"])
    def test_empty_content_rejected(self, bad_content):
        with pytest.raises(ValidationError, match="不能为空"):
            MemoryEntry(**make_entry(content=bad_content))

    def test_overlong_content_rejected(self):
        with pytest.raises(ValidationError, match="超过上限"):
            MemoryEntry(**make_entry(content="x" * (CONTENT_MAX_CHARS + 1)))


class TestDetailField:
    """detail：可选的 Enhanced-Notes 段落（带前因后果的 2-4 句话）。"""

    def test_detail_defaults_to_none(self):
        assert MemoryEntry(**make_entry()).detail is None

    def test_detail_is_stripped(self):
        entry = MemoryEntry(**make_entry(detail="  带空白的段落。  "))
        assert entry.detail == "带空白的段落。"

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_blank_detail_becomes_none(self, blank):
        assert MemoryEntry(**make_entry(detail=blank)).detail is None

    def test_overlong_detail_rejected(self):
        with pytest.raises(ValidationError, match="超过上限"):
            MemoryEntry(**make_entry(detail="x" * (DETAIL_MAX_CHARS + 1)))

    def test_detail_at_max_length_ok(self):
        entry = MemoryEntry(**make_entry(detail="x" * DETAIL_MAX_CHARS))
        assert len(entry.detail) == DETAIL_MAX_CHARS

    def test_index_text_concatenates_detail(self):
        entry = MemoryEntry(**make_entry(content="原子句。", detail="完整段落。"))
        assert entry.index_text == "原子句。\n完整段落。"

    def test_index_text_without_detail_is_content(self):
        entry = MemoryEntry(**make_entry(content="原子句。"))
        assert entry.index_text == "原子句。"


class TestProfileConfidenceRule:
    def test_profile_low_confidence_rejected(self):
        with pytest.raises(ValidationError, match="profile"):
            MemoryEntry(**make_entry(memory_type="profile", confidence="low"))

    @pytest.mark.parametrize("conf", ["high", "medium"])
    def test_profile_high_medium_accepted(self, conf):
        entry = MemoryEntry(**make_entry(memory_type="profile", confidence=conf))
        assert entry.confidence == conf

    def test_low_confidence_ok_for_other_types(self):
        for mt in ["semantic", "procedural", "episodic"]:
            entry = MemoryEntry(**make_entry(memory_type=mt, confidence="low"))
            assert entry.confidence == "low"

    def test_invalid_memory_type_rejected(self):
        with pytest.raises(ValidationError):
            MemoryEntry(**make_entry(memory_type="declarative"))

    def test_invalid_confidence_rejected(self):
        with pytest.raises(ValidationError):
            MemoryEntry(**make_entry(confidence="certain"))


class TestMemoryProposal:
    def test_valid_proposal(self):
        proposal = MemoryProposal(
            candidates=[MemoryEntry(**make_entry())],
            falsifiable_contract="若用户在后续会话中改用 pip，则驳回本提案。",
            created_by="distiller-v0",
        )
        assert len(proposal.candidates) == 1

    @pytest.mark.parametrize("field", ["falsifiable_contract", "created_by"])
    def test_empty_text_fields_rejected(self, field):
        kwargs = {
            "candidates": [MemoryEntry(**make_entry())],
            "falsifiable_contract": "契约文本",
            "created_by": "distiller-v0",
        }
        kwargs[field] = "   "
        with pytest.raises(ValidationError, match="不能为空"):
            MemoryProposal(**kwargs)

    def test_invalid_candidate_propagates_error(self):
        with pytest.raises(ValidationError):
            MemoryProposal(
                candidates=[MemoryEntry(**make_entry(id="Bad_Id"))],
                falsifiable_contract="契约文本",
                created_by="distiller-v0",
            )


class TestNormalizeEntryId:
    """normalize_entry_id：LLM 自由文本 id 的规范化（通病 A 修复，layer2-03 根因）。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("python-version-upgrade-to-3.12", "python-version-upgrade-to-3-12"),
            ("Bad ID!", "bad-id"),
            ("checkout_v2_enabled", "checkout-v2-enabled"),
            ("--leading-and-trailing--", "leading-and-trailing"),
            ("a--b---c", "a-b-c"),
            ("already-kebab-case", "already-kebab-case"),
            ("!!!", ""),  # 全是非法字符 → 空串，调用方兜底（进复核队列）
        ],
    )
    def test_normalization(self, raw, expected):
        from agent_memory.models import normalize_entry_id

        assert normalize_entry_id(raw) == expected


class TestRetrievalCount:
    """retrieval_count（M4a）：检索命中计数，hybrid 命中时 +1。"""

    def test_defaults_to_zero(self):
        entry = MemoryEntry(**make_entry())
        assert entry.retrieval_count == 0

    def test_explicit_value_accepted(self):
        entry = MemoryEntry(**make_entry(retrieval_count=7))
        assert entry.retrieval_count == 7

    def test_negative_rejected(self):
        with pytest.raises(ValidationError):
            MemoryEntry(**make_entry(retrieval_count=-1))


class TestEvolutionProposal:
    """M4a 整理提案 schema：变更带可证伪契约，kind 与载荷一致。"""

    def _contract(self, **overrides):
        fields = {
            "evidence": "证据",
            "root_cause": "根因",
            "expected_fix": "预期修复",
            "blast_radius": "受损面",
        }
        fields.update(overrides)
        return fields

    def _proposal(self, changes):
        return EvolutionProposal(
            id="evolve-20260820-ab12cd",
            changes=changes,
            falsifiable_contract="整体契约",
            created_by="evolve-cycle",
            created_at=datetime(2026, 8, 20, 12, 0, 0),
        )

    def test_valid_merge_change(self):
        change = EvolutionChange(
            kind="merge",
            target_ids=["uv-a", "uv-b"],
            merged_entry=MemoryEntry(**make_entry(id="uv-c")),
            reason="重复",
            contract=FalsifiableContract(**self._contract()),
        )
        proposal = self._proposal([change])
        assert proposal.changes[0].merged_entry.id == "uv-c"

    def test_merge_without_merged_entry_rejected(self):
        with pytest.raises(ValidationError, match="merged_entry"):
            EvolutionChange(
                kind="merge", target_ids=["uv-a", "uv-b"],
                reason="重复", contract=FalsifiableContract(**self._contract()),
            )

    def test_downgrade_without_new_confidence_rejected(self):
        with pytest.raises(ValidationError, match="new_confidence"):
            EvolutionChange(
                kind="downgrade", target_ids=["uv-a"],
                reason="冷条目", contract=FalsifiableContract(**self._contract()),
            )

    def test_empty_target_ids_rejected(self):
        with pytest.raises(ValidationError):
            EvolutionChange(
                kind="archive", target_ids=[],
                reason="冷条目", contract=FalsifiableContract(**self._contract()),
            )

    @pytest.mark.parametrize(
        "field", ["evidence", "root_cause", "expected_fix", "blast_radius"]
    )
    def test_empty_contract_field_rejected(self, field):
        with pytest.raises(ValidationError, match="不能为空"):
            FalsifiableContract(**self._contract(**{field: "  "}))

    def test_proposal_id_must_be_kebab(self):
        with pytest.raises(ValidationError, match="kebab-case"):
            EvolutionProposal(
                id="Bad Id", changes=[], falsifiable_contract="契约",
                created_by="test", created_at=datetime(2026, 8, 20),
            )
