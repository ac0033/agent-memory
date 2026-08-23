"""consolidate.py 测试：四类整理动作产出提案，且提案绝不直接改记忆层。

向量构造沿用 conftest.FakeEmbedder：共享关键词 → cosine 距离 0（重复对），
不含共同关键词 → 正交（距离 1.0）。
"""

from datetime import datetime, timedelta

import pytest
import yaml

from agent_memory.config import Settings
from agent_memory.llm import LLMError
from agent_memory.long_term.evolve.consolidate import build_proposal, save_proposal
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import EvidenceRef

NOW = datetime(2026, 8, 20, 12, 0, 0)
OLD_DATE = (NOW - timedelta(days=120)).date()


class ScriptedLLM:
    """脚本化 fake LLM：按 system 提示词分流合并判定 / 离线复核判定。"""

    def __init__(self, pair_verdicts=None, stale_verdicts=None, error=False):
        # pair_verdicts: {(a_id, b_id): {...}}，user 里两个 id 都出现即命中
        self.pair_verdicts = pair_verdicts or {}
        self.stale_verdicts = stale_verdicts or {}
        self.error = error

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        if self.error:
            raise LLMError("模拟 LLM 连续失败")
        if "离线复核员" in system:
            for nid, verdict in self.stale_verdicts.items():
                if f"id: {nid}" in user:
                    return verdict
            return {"verdict": "VALID", "reason": "默认仍成立"}
        for (a_id, b_id), verdict in self.pair_verdicts.items():
            if f"id: {a_id}" in user and f"id: {b_id}" in user:
                return verdict
        return {"verdict": "UNRELATED", "reason": "默认无关"}


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


def _build(components, llm):
    _, store, index, embedder, settings = components
    return build_proposal(store, index, embedder, settings, llm, now=NOW)


def _kinds(proposal):
    return {c.kind for c in proposal.changes}


class TestMerge:
    def test_merge_pair_produces_merge_change(self, entry_factory, components):
        _, store, index, embedder, _ = components
        a = entry_factory(
            entry_id="uv-manager", content="项目包管理器用 uv。", version=3
        ).model_copy(update={
            "evidence": [EvidenceRef(session_id="s1", source="cli")],
            "retrieval_count": 2,
        })
        b = entry_factory(
            entry_id="uv-deps", content="该项目使用 uv 管理依赖。"
        ).model_copy(update={
            "evidence": [EvidenceRef(session_id="s2", source="mcp")],
            "retrieval_count": 5,
        })
        _seed(store, index, embedder, a)
        _seed(store, index, embedder, b)
        llm = ScriptedLLM(pair_verdicts={
            ("uv-manager", "uv-deps"): {
                "verdict": "MERGE",
                "merged_id": "uv-tooling",
                "merged_content": "项目用 uv 做包管理与依赖管理。",
                "merged_detail": None,
                "reason": "同一事实重复沉淀",
            }
        })

        proposal = _build(components, llm)

        assert _kinds(proposal) == {"merge"}
        change = proposal.changes[0]
        assert set(change.target_ids) == {"uv-manager", "uv-deps"}
        merged = change.merged_entry
        assert merged.id == "uv-tooling"
        assert merged.content == "项目用 uv 做包管理与依赖管理。"
        assert merged.version == 4  # max(3, 1) + 1
        assert merged.supersedes == "uv-deps"  # 主条目 = 按 id 排序的前者（确定性）
        assert merged.retrieval_count == 7  # 2 + 5
        assert {e.session_id for e in merged.evidence} == {"s1", "s2"}  # evidence 并集
        # 可证伪契约四要素齐备
        assert change.contract.evidence and change.contract.root_cause
        assert change.contract.expected_fix and change.contract.blast_radius

    def test_proposal_never_touches_memory_layer(self, entry_factory, components, tmp_path):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="依赖管理用 uv。"))
        llm = ScriptedLLM(pair_verdicts={
            ("uv-a", "uv-b"): {
                "verdict": "MERGE", "merged_id": "uv-c",
                "merged_content": "包管理与依赖管理都用 uv。",
                "merged_detail": None, "reason": "重复",
            }
        })

        proposal = _build(components, llm)
        proposal_dir = save_proposal(proposal, tmp_path)

        # 记忆层原样保留，提案落在 review_queue/evolution/<ts>/
        assert {e.id for e in store.list()} == {"uv-a", "uv-b"}
        assert proposal_dir.parent == tmp_path / "review_queue" / "evolution"
        saved = yaml.safe_load((proposal_dir / "proposal.yaml").read_text(encoding="utf-8"))
        assert saved["id"] == proposal.id
        assert len(saved["changes"]) == 1

    def test_conflict_pair_marked_for_human_not_merged(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="包管理器已弃用 uv。"))
        llm = ScriptedLLM(pair_verdicts={
            ("uv-a", "uv-b"): {"verdict": "CONFLICT", "reason": "双方各有证据"}
        })

        proposal = _build(components, llm)

        assert _kinds(proposal) == {"conflict"}
        assert proposal.changes[0].merged_entry is None
        assert {e.id for e in store.list()} == {"uv-a", "uv-b"}  # 不强行收敛

    def test_unrelated_pair_no_change(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="依赖安装用 uv。"))
        llm = ScriptedLLM()  # 默认 UNRELATED
        proposal = _build(components, llm)
        assert proposal.changes == []

    def test_distant_entries_not_judged(self, entry_factory, components):
        # 正交向量（距离 1.0 > 阈值）不进入 LLM 判定
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="tz-b", content="服务器时区是 UTC。"))

        class SpyLLM(ScriptedLLM):
            calls = 0

            def complete_json(self, system, user, schema_description):
                if "整理员" in system:
                    SpyLLM.calls += 1
                return super().complete_json(system, user, schema_description)

        proposal = _build(components, SpyLLM())
        assert SpyLLM.calls == 0
        assert proposal.changes == []

    def test_invalid_merge_output_degrades_to_revise(self, entry_factory, components):
        """LLM 合并产出非法（空 content）时不丢弃，降级为 revise 交人工。"""
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="依赖管理用 uv。"))
        llm = ScriptedLLM(pair_verdicts={
            ("uv-a", "uv-b"): {
                "verdict": "MERGE", "merged_id": "uv-c",
                "merged_content": "", "merged_detail": None, "reason": "坏产出",
            }
        })
        proposal = _build(components, llm)
        assert _kinds(proposal) == {"revise"}
        assert "非法" in proposal.changes[0].reason

    def test_llm_error_becomes_revise(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="依赖管理用 uv。"))
        proposal = _build(components, ScriptedLLM(error=True))
        # 合并判定失败 → revise；两条被 consume，后续抽查跳过
        assert _kinds(proposal) == {"revise"}
        assert "不可用" in proposal.changes[0].reason


class TestStaleRecheck:
    """离线抽查复核（propagate.judge_validity）：抽查最旧条目本身是否仍成立。"""

    def test_invalidated_old_entry_proposed_for_removal(self, entry_factory, components):
        _, store, index, embedder, _ = components
        old = entry_factory(
            entry_id="old-fact", content="排查启动失败先看 Windows 端口占用。",
            last_verified=OLD_DATE,
        )
        _seed(store, index, embedder, old)
        llm = ScriptedLLM(stale_verdicts={
            "old-fact": {"verdict": "INVALIDATED", "reason": "前提已失效"}
        })
        proposal = _build(components, llm)
        assert _kinds(proposal) == {"invalidate"}
        assert proposal.changes[0].target_ids == ["old-fact"]
        assert "Windows" in store.get("old-fact").content  # 提案不删库

    def test_needs_revision_goes_to_human(self, entry_factory, components):
        """证据不足以确认失效时判 NEEDS_REVISION → revise 交人工，不判失效。"""
        _, store, index, embedder, _ = components
        old = entry_factory(
            entry_id="old-fact", content="Windows 上时区要手动设置。", last_verified=OLD_DATE
        )
        _seed(store, index, embedder, old)
        llm = ScriptedLLM(stale_verdicts={
            "old-fact": {"verdict": "NEEDS_REVISION", "reason": "疑似过期但证据不足"}
        })
        proposal = _build(components, llm)
        assert _kinds(proposal) == {"revise"}

    def test_unaffected_entry_no_change(self, entry_factory, components):
        _, store, index, embedder, _ = components
        old = entry_factory(
            entry_id="old-fact", content="Windows 是主力开发机。", last_verified=OLD_DATE
        )
        _seed(store, index, embedder, old)
        proposal = _build(components, ScriptedLLM())  # 默认 VALID
        assert proposal.changes == []

    def test_healthy_entry_not_invalidated_by_recheck(self, entry_factory, components):
        """语义边界：抽查复核不复用传播判定，正常记忆不得被判失效。

        回归缺陷：旧实现把 judge_propagation(old=entry, new=None, neighbor=entry)
        指向条目自身，prompt 声称"旧事实已被删除"，正常记忆恒判 INVALIDATED。
        这里断言复核 prompt 不含传播语义措辞，且默认判定（VALID）不产生变更。
        """
        _, store, index, embedder, _ = components
        healthy = entry_factory(
            entry_id="python-version", content="仓库使用 Python 3.12。",
            last_verified=OLD_DATE,
        )
        _seed(store, index, embedder, healthy)

        class SpyLLM(ScriptedLLM):
            calls: list[tuple[str, str]] = []

            def complete_json(self, system, user, schema_description):
                SpyLLM.calls.append((system, user))
                return super().complete_json(system, user, schema_description)

        proposal = _build(components, SpyLLM())

        assert proposal.changes == []  # 正常记忆不产生 invalidate/revise
        assert len(SpyLLM.calls) == 1
        system, user = SpyLLM.calls[0]
        assert "离线复核员" in system
        assert "默认假设" in system  # 默认成立，明确证据才判失效
        assert "旧事实已被删除" not in system
        assert "旧事实已被删除" not in user
        assert "python-version" in user and "Python 3.12" in user  # 复核对象是条目自身

    def test_unparseable_validity_verdict_defaults_to_valid(self, entry_factory, components):
        """LLM 输出无法解析时按 VALID 处理（fail-safe：默认不动，不错判失效）。"""
        _, store, index, embedder, _ = components
        old = entry_factory(
            entry_id="old-fact", content="Windows 是主力开发机。", last_verified=OLD_DATE
        )
        _seed(store, index, embedder, old)
        llm = ScriptedLLM(stale_verdicts={
            "old-fact": {"verdict": "garbage", "reason": "无法解析"}
        })
        proposal = _build(components, llm)
        assert proposal.changes == []

    def test_recheck_prompt_includes_neighbors(self, entry_factory, components):
        """复核判定能看到库中语义近邻（供对照"是否被更新条目取代"）。"""
        _, store, index, embedder, _ = components
        old = entry_factory(
            entry_id="port-old", content="dev server 端口固定 8765。",
            last_verified=OLD_DATE,
        )
        newer = entry_factory(entry_id="port-new", content="dev server 端口已改为 8766。")
        _seed(store, index, embedder, old)
        _seed(store, index, embedder, newer)

        class SpyLLM(ScriptedLLM):
            users: list[str] = []

            def complete_json(self, system, user, schema_description):
                if "离线复核员" in system:
                    SpyLLM.users.append(user)
                return super().complete_json(system, user, schema_description)

        _build(components, SpyLLM())

        assert len(SpyLLM.users) == 2  # 两条都在最旧抽查样本内
        old_prompt = next(
            u for u in SpyLLM.users if "待复核的记忆：\n- id: port-old" in u
        )
        assert "port-new" in old_prompt  # 近邻进入对照上下文


class TestColdEntries:
    """长期未被检索（retrieval_count==0 且创建超 stale_days）的降权/归档建议。"""

    def _cold(self, entry_factory, confidence):
        return entry_factory(entry_id="cold-one", confidence=confidence).model_copy(
            update={"created_at": OLD_DATE, "retrieval_count": 0}
        )

    def test_high_confidence_cold_entry_downgrade(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, self._cold(entry_factory, "high"))
        proposal = _build(components, ScriptedLLM())
        assert _kinds(proposal) == {"downgrade"}
        assert proposal.changes[0].new_confidence == "medium"

    def test_low_confidence_cold_entry_archive(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, self._cold(entry_factory, "low"))
        proposal = _build(components, ScriptedLLM())
        assert _kinds(proposal) == {"archive"}

    def test_retrieved_entry_ignored(self, entry_factory, components):
        _, store, index, embedder, _ = components
        entry = self._cold(entry_factory, "high").model_copy(update={"retrieval_count": 3})
        _seed(store, index, embedder, entry)
        proposal = _build(components, ScriptedLLM())
        assert proposal.changes == []

    def test_recent_cold_entry_ignored(self, entry_factory, components):
        # 创建未超 stale_days（90 天），即使从未被检索也不动
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="fresh-one"))
        proposal = _build(components, ScriptedLLM())
        assert proposal.changes == []

    def test_cold_entry_consumed_by_merge_not_double_counted(self, entry_factory, components):
        _, store, index, embedder, _ = components
        a = self._cold(entry_factory, "high").model_copy(
            update={"content": "包管理器用 uv。"}
        )
        b = entry_factory(entry_id="uv-b", content="依赖管理用 uv。").model_copy(
            update={"created_at": OLD_DATE, "retrieval_count": 0}
        )
        _seed(store, index, embedder, a)
        _seed(store, index, embedder, b)
        llm = ScriptedLLM(pair_verdicts={
            ("cold-one", "uv-b"): {
                "verdict": "MERGE", "merged_id": "uv-c",
                "merged_content": "包管理与依赖管理都用 uv。",
                "merged_detail": None, "reason": "重复",
            }
        })
        proposal = _build(components, llm)
        assert _kinds(proposal) == {"merge"}  # 不再追加 downgrade/archive
