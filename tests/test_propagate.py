"""propagate.py 测试：变更传播的三种判定分支 + fail-safe 兜底。

近邻构造依赖 conftest.FakeEmbedder 的关键词向量：共享关键词 → cosine 距离 0
（近邻），不含共同关键词 → 正交（距离 1.0，非近邻）。procedural 条目无论
距离远近都会并入判定。
"""

import json

import pytest
import yaml

from agent_memory.config import Settings
from agent_memory.ingest.propagate import (
    judge_propagation,
    judge_validity,
    propagate_change,
)
from agent_memory.llm import LLMError
from agent_memory.store.index_db import IndexDB
from agent_memory.store.markdown_store import MarkdownStore


class VerdictLLM:
    """按既有记忆 id 返回脚本化传播判定的 fake LLM。"""

    def __init__(self, verdicts: dict[str, dict] | None = None, error: bool = False):
        self.verdicts = verdicts or {}
        self.error = error

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        if self.error:
            raise LLMError("模拟传播判定 LLM 连续失败")
        for nid, verdict in self.verdicts.items():
            if f"id: {nid}" in user:
                return verdict
        return {"verdict": "UNAFFECTED", "reason": "默认不受影响"}


@pytest.fixture
def components(tmp_path, fake_embedder):
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    settings = Settings(data_dir=tmp_path)
    yield tmp_path, store, index, fake_embedder, settings
    index.close()


def _seed(store, index, embedder, entry):
    store.create(entry)
    index.upsert(entry, embedder.embed_texts([entry.content])[0])


def _propagate(components, llm, old, new=None, exclude_ids=()):
    _, store, index, embedder, settings = components
    return propagate_change(
        store, index, embedder, settings, llm,
        old=old, new=new, exclude_ids=set(exclude_ids),
    )


def _old_new(entry_factory):
    old = entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。")
    new = entry_factory(entry_id="port-new", content="本项目 dev server 端口已改为 8766。")
    return old, new


def test_invalidated_neighbor_deleted_with_audit(entry_factory, components):
    tmp_path, store, index, embedder, _ = components
    old, new = _old_new(entry_factory)
    neighbor = entry_factory(
        entry_id="check-port-first",
        content="排查启动失败时先看 dev server 端口 8765 是否被占用。",
        memory_type="procedural",
    )
    _seed(store, index, embedder, neighbor)
    llm = VerdictLLM({"check-port-first": {"verdict": "INVALIDATED", "reason": "前提已失效"}})

    report = _propagate(components, llm, old, new)

    assert report.invalidated == ["check-port-first"]
    with pytest.raises(KeyError):
        store.get("check-port-first")  # 已从记忆层删除
    # 审计日志留下完整快照（版本历史）
    log_lines = (tmp_path / "logs" / "propagation.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    record = json.loads(log_lines[0])
    assert record["event"] == "propagated_invalidation"
    assert record["removed"]["id"] == "check-port-first"
    assert record["trigger_new"]["id"] == "port-new"
    assert report.queued_files == []  # 已自动处置，不进复核队列


def test_needs_revision_queued_not_deleted(entry_factory, components):
    tmp_path, store, index, embedder, _ = components
    old, new = _old_new(entry_factory)
    neighbor = entry_factory(
        entry_id="deploy-port-note",
        content="部署文档里写的端口 8765 可能需要同步更新。",
    )
    _seed(store, index, embedder, neighbor)
    llm = VerdictLLM({"deploy-port-note": {"verdict": "NEEDS_REVISION", "reason": "证据不足"}})

    report = _propagate(components, llm, old, new)

    assert report.needs_revision == ["deploy-port-note"]
    assert store.get("deploy-port-note") is not None  # 不强行收敛，保留待人工修订
    assert len(report.queued_files) == 1
    payload = yaml.safe_load(report.queued_files[0].read_text(encoding="utf-8"))
    assert payload["entry"]["id"] == "deploy-port-note"
    assert "传播" in payload["reason"]


def test_unaffected_neighbor_untouched(entry_factory, components):
    tmp_path, store, index, embedder, _ = components
    old, new = _old_new(entry_factory)
    neighbor = entry_factory(
        entry_id="uv-preference", content="用户偏好用 uv 管理 Python 环境。"  # 与端口正交
    )
    _seed(store, index, embedder, neighbor)
    llm = VerdictLLM()  # 默认 UNAFFECTED

    report = _propagate(components, llm, old, new)

    assert report.invalidated == [] and report.needs_revision == []
    assert store.get("uv-preference") is not None


def test_procedural_judged_even_when_distant(entry_factory, components):
    """procedural 条目不共享关键词（向量正交）也要并入判定——前提依赖常被措辞掩盖。"""
    _, store, index, embedder, _ = components
    old, new = _old_new(entry_factory)
    neighbor = entry_factory(
        entry_id="triage-runbook",
        content="排查线上问题时先按 runbook 逐项确认再上报。",
        memory_type="procedural",
    )
    _seed(store, index, embedder, neighbor)
    seen: list[str] = []

    class SpyLLM(VerdictLLM):
        def complete_json(self, system, user, schema_description):
            seen.append(user)
            return super().complete_json(system, user, schema_description)

    report = _propagate(components, SpyLLM(), old, new)

    assert any("triage-runbook" in u for u in seen)  # 被提交判定
    assert report.unaffected == 1


def test_llm_failure_goes_to_queue_not_deleted(entry_factory, components):
    """fail-safe：判定 LLM 失败时不动作、进复核队列，绝不错删。"""
    _, store, index, embedder, _ = components
    old, new = _old_new(entry_factory)
    neighbor = entry_factory(
        entry_id="check-port-first",
        content="排查启动失败时先看 dev server 端口 8765 是否被占用。",
        memory_type="procedural",
    )
    _seed(store, index, embedder, neighbor)

    report = _propagate(components, VerdictLLM(error=True), old, new)

    assert len(report.failed) == 1
    assert store.get("check-port-first") is not None  # 未删
    assert len(report.queued_files) == 1


def test_unparseable_verdict_defaults_to_unaffected(entry_factory):
    """判定输出无法解析（如脚本化 oracle 的 ADD 响应）按 UNAFFECTED 处理。"""
    class GarbageLLM(VerdictLLM):
        def complete_json(self, system, user, schema_description):
            return {"action": "ADD", "target_id": None, "reason": "oracle 默认"}

    old, new = _old_new(entry_factory)
    neighbor = entry_factory(entry_id="some-entry", content="本项目 dev server 端口固定 8765。")
    verdict = judge_propagation(GarbageLLM(), old, new, neighbor)
    assert verdict["verdict"] == "UNAFFECTED"


def test_change_parties_excluded(entry_factory, components):
    """变更双方（old/new）不参与传播判定。"""
    _, store, index, embedder, _ = components
    old, new = _old_new(entry_factory)
    _seed(store, index, embedder, new)  # new 已入库（reconcile 先落库再传播）
    bystander = entry_factory(
        entry_id="port-bystander", content="文档站点的端口固定 4321，与 dev server 无关。"
    )
    _seed(store, index, embedder, bystander)
    seen: list[str] = []

    class SpyLLM(VerdictLLM):
        def complete_json(self, system, user, schema_description):
            seen.append(user)
            return super().complete_json(system, user, schema_description)

    _propagate(components, SpyLLM(), old, new, exclude_ids={old.id, new.id})
    # new 与 old 距离为 0，若不排除会被提交判定；排除后只有旁观者进判定
    assert len(seen) == 1
    assert "port-bystander" in seen[0]


class TestJudgeValidity:
    """judge_validity：离线抽查复核条目自身是否仍成立（M4 整理循环用）。

    语义与 judge_propagation 不同：propagation 回答"某条记忆被撤销后牵连谁"，
    validity 回答"这条记忆本身还成立吗"——默认假设成立，只有明确证据才判
    INVALIDATED，证据不足判 NEEDS_REVISION 交人工。
    """

    def test_prompt_does_not_frame_entry_as_deleted(self, entry_factory):
        """复核 prompt 不得含传播语义（"旧事实已被删除"），复核对象是条目自身。"""
        seen: list[tuple[str, str]] = []

        class SpyLLM(VerdictLLM):
            def complete_json(self, system, user, schema_description):
                seen.append((system, user))
                return {"verdict": "VALID", "reason": "无失效证据"}

        entry = entry_factory(entry_id="py-version", content="仓库使用 Python 3.12。")
        verdict = judge_validity(SpyLLM(), entry, neighbors=[])

        assert verdict["verdict"] == "VALID"
        system, user = seen[0]
        assert "离线复核员" in system
        assert "默认假设" in system
        assert "旧事实已被删除" not in system
        assert "旧事实已被删除" not in user
        assert "py-version" in user and "Python 3.12" in user

    def test_unparseable_verdict_defaults_to_valid(self, entry_factory):
        """输出无法解析按 VALID 处理（fail-safe：宁可漏判也不错判失效）。"""
        class GarbageLLM(VerdictLLM):
            def complete_json(self, system, user, schema_description):
                return {"action": "ADD", "reason": "oracle 默认"}

        entry = entry_factory(entry_id="py-version", content="仓库使用 Python 3.12。")
        verdict = judge_validity(GarbageLLM(), entry, neighbors=[])
        assert verdict["verdict"] == "VALID"

    def test_clear_supersede_evidence_invalidates(self, entry_factory):
        """近邻里有对同一事实给出更新取值的条目 → 明确证据，判 INVALIDATED。"""
        entry = entry_factory(entry_id="port-old", content="dev server 端口固定 8765。")
        newer = entry_factory(entry_id="port-new", content="dev server 端口已改为 8766。")
        llm = VerdictLLM({
            "port-old": {"verdict": "INVALIDATED", "reason": "已被 port-new 取代"}
        })
        verdict = judge_validity(llm, entry, neighbors=[newer])
        assert verdict["verdict"] == "INVALIDATED"

    def test_insufficient_evidence_is_revision_not_invalidation(self, entry_factory):
        """疑似过期但证据不足 → NEEDS_REVISION（交人工），不得直接判失效。"""
        entry = entry_factory(entry_id="tz-note", content="服务器时区可能还是 UTC。")
        llm = VerdictLLM({
            "tz-note": {"verdict": "NEEDS_REVISION", "reason": "疑似过期但无对照证据"}
        })
        verdict = judge_validity(llm, entry, neighbors=[])
        assert verdict["verdict"] == "NEEDS_REVISION"
