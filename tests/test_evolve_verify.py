"""verify.py 测试：三档验证（boundary / retention / safety）各自一票否决。"""

from datetime import datetime

import pytest

from agent_memory.config import Settings
from agent_memory.llm import LLMError
from agent_memory.long_term.evolve.verify import TierResult, VerifyReport, verify_proposal
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import (
    EvolutionChange,
    EvolutionProposal,
    FalsifiableContract,
)

NOW = datetime(2026, 8, 20, 12, 0, 0)


class BoundaryLLM:
    """只回答 boundary 判定的 fake LLM。"""

    def __init__(self, passed=True, error=False):
        self.passed = passed
        self.error = error

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        if self.error:
            raise LLMError("模拟 boundary 判定连续失败")
        assert "验证员" in system  # 只应收到 boundary 判定调用
        return {"pass": self.passed, "reason": "脚本化判定"}


@pytest.fixture
def components(tmp_path, fake_embedder):
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    settings = Settings(data_dir=tmp_path)
    yield tmp_path, store, index, fake_embedder, settings
    index.close()


def _seed(store, index, embedder, entry):
    store.create(entry)
    index.upsert(entry, embedder.embed_texts([entry.index_text])[0])


def _contract():
    return FalsifiableContract(
        evidence="证据", root_cause="根因", expected_fix="预期修复", blast_radius="受损面"
    )


def _proposal(changes):
    return EvolutionProposal(
        id="evolve-20260820-test01",
        changes=changes,
        falsifiable_contract="整体契约",
        created_by="test",
        created_at=NOW,
    )


def _archive_change(target_id):
    return EvolutionChange(
        kind="archive", target_ids=[target_id], reason="长期未检索", contract=_contract()
    )


def _verify(components, proposal, llm):
    _, store, index, embedder, settings = components
    return verify_proposal(proposal, store, index, embedder, settings, llm)


class TestThreeTierVeto:
    """三档分开判定，任一不过则整体不晋升。"""

    def test_all_pass(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        _seed(store, index, embedder, entry_factory(entry_id="alive", content="服务器时区是 UTC。"))
        proposal = _proposal([_archive_change("cold")])
        report = _verify(components, proposal, BoundaryLLM(passed=True))
        assert report.passed
        assert report.boundary.passed and report.retention.passed and report.safety.passed

    def test_boundary_veto(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        _seed(store, index, embedder, entry_factory(entry_id="alive", content="服务器时区是 UTC。"))
        proposal = _proposal([_archive_change("cold")])
        report = _verify(components, proposal, BoundaryLLM(passed=False))
        assert not report.passed
        assert not report.boundary.passed
        assert report.retention.passed and report.safety.passed  # 其余档不受影响

    def test_boundary_llm_error_fails_closed(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        proposal = _proposal([_archive_change("cold")])
        report = _verify(components, proposal, BoundaryLLM(error=True))
        assert not report.passed
        assert "fail-closed" in report.boundary.reason

    def test_safety_veto(self, entry_factory, components):
        """提案不得删除/弱化 safety 相关记忆。"""
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(
            entry_id="safety-rule", content="安全红线：绝不把密钥写进代码仓库。"
        ))
        _seed(store, index, embedder, entry_factory(entry_id="alive", content="服务器时区是 UTC。"))
        proposal = _proposal([_archive_change("safety-rule")])
        report = _verify(components, proposal, BoundaryLLM(passed=True))
        assert not report.passed
        assert not report.safety.passed
        assert "safety-rule" in report.safety.reason

    def test_safety_allows_human_review_kinds(self, entry_factory, components):
        """conflict / revise 是人工裁决项、不会被自动应用，不视为触碰 safety。"""
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(
            entry_id="safety-rule", content="安全红线：绝不把密钥写进代码仓库。"
        ))
        _seed(store, index, embedder, entry_factory(entry_id="other", content="端口固定 8765。"))
        proposal = _proposal([
            EvolutionChange(
                kind="conflict", target_ids=["safety-rule", "other"],
                reason="矛盾待人工裁决", contract=_contract(),
            )
        ])
        report = _verify(components, proposal, BoundaryLLM(passed=True))
        assert report.safety.passed

    def test_retention_veto(self, entry_factory, components):
        """合并把无关键条目挤出基准 query 的 top5 → retention 否决。

        场景：e-1..e-5 都含 "uv"（向量同向），t-1/t-2 含 "时区"（正交）。
        基准 query = 各 e-* 的 content；合并 t-1+t-2 产出的 merged 也含 "uv" 且
        content 完整包含 e-1.content（FTS 命中），RRF 重排后有 e-* 掉出 top5。
        """
        _, store, index, embedder, _ = components
        for i in range(1, 6):
            content = "uv 是包管理器" if i == 1 else f"uv 的第 {i} 种用法。"
            _seed(store, index, embedder, entry_factory(entry_id=f"e-{i}", content=content))
        _seed(store, index, embedder, entry_factory(entry_id="t-1", content="服务器时区是 UTC。"))
        _seed(store, index, embedder, entry_factory(entry_id="t-2", content="时区配置文件路径。"))
        merged = entry_factory(
            entry_id="uv-merged", content="uv 是包管理器，也负责依赖解析。"
        )
        proposal = _proposal([
            EvolutionChange(
                kind="merge", target_ids=["t-1", "t-2"], merged_entry=merged,
                reason="重复", contract=_contract(),
            )
        ])
        report = _verify(components, proposal, BoundaryLLM(passed=True))
        assert not report.passed
        assert not report.retention.passed
        # 有提案未涉及的条目（e-* 之一）掉出基准 query 的 top5
        assert "丢失" in report.retention.reason
        assert any(f"e-{i}" in report.retention.reason for i in range(1, 6))


class TestBoundaryConflictSemantics:
    """boundary 档对 conflict/revise（转人工）变更的评估语义。

    回归缺陷：旧 prompt 让 boundary 以"conflict 只标记未解决、矛盾场景仍
    复现"为由否决整份提案——但 conflict 的设计语义就是交人工裁决（apply
    一律跳过），标记准确即视为该部分通过，不应成为否决理由。
    """

    def _conflict_proposal(self):
        return _proposal([
            EvolutionChange(
                kind="conflict", target_ids=["uv-a", "uv-b"],
                reason="对同一事实取值矛盾且各有证据，交人工裁决",
                contract=_contract(),
            )
        ])

    def test_pure_conflict_proposal_not_vetoed(self, entry_factory, components):
        """纯 conflict 提案：标记准确即通过 boundary，不被一票否决。"""
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="包管理器已弃用 uv。"))
        report = _verify(components, self._conflict_proposal(), BoundaryLLM(passed=True))
        assert report.passed

    def test_boundary_prompt_marks_human_review_kinds(self, entry_factory, components):
        """交给 boundary 的 prompt 明确区分自动应用 / 转人工两类变更。"""
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="包管理器已弃用 uv。"))
        seen: list[tuple[str, str]] = []

        class SpyLLM(BoundaryLLM):
            def complete_json(self, system, user, schema_description):
                seen.append((system, user))
                return super().complete_json(system, user, schema_description)

        _verify(components, self._conflict_proposal(), SpyLLM(passed=True))

        assert len(seen) == 1
        system, user = seen[0]
        # system 明确：conflict/revise 转人工是设计语义，标记准确即通过
        assert "转人工" in system
        assert "不是否决理由" in system
        # user 里该变更被标注为转人工项，不与自动应用类混为一谈
        assert "[conflict]（转人工，apply 跳过）" in user


class TestVerifyReport:
    def test_to_dict_shape(self):
        report = VerifyReport(
            boundary=TierResult(True, "ok"),
            retention=TierResult(False, "退化"),
            safety=TierResult(True, "ok"),
        )
        d = report.to_dict()
        assert d["passed"] is False
        assert d["retention"] == {"passed": False, "reason": "退化"}
