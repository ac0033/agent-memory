"""cycle.py 测试：五步循环编排——触发 → 提案 → （dry-run 停）→ 验证 → 晋升/归档。"""

import json
from datetime import datetime

import pytest

from agent_memory.config import Settings
from agent_memory.llm import LLMError
from agent_memory.long_term.evolve.cycle import (
    load_last_run_at,
    run_evolution_cycle,
    save_last_run_at,
)
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore

NOW = datetime(2026, 8, 20, 12, 0, 0)


class CycleLLM:
    """脚本化 fake LLM：按 system 提示词分流 合并判定 / 传播复核 / boundary 验证。"""

    def __init__(self, boundary_pass=True):
        self.boundary_pass = boundary_pass

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        if "传播判定员" in system:
            return {"verdict": "UNAFFECTED", "reason": "仍成立"}
        if "验证员" in system:
            if self.boundary_pass is None:
                raise LLMError("模拟 boundary 判定失败")
            return {"pass": self.boundary_pass, "reason": "脚本化判定"}
        # 合并判定：两条 "uv" 重复记忆 → MERGE
        if "id: uv-a" in user and "id: uv-b" in user:
            return {
                "verdict": "MERGE",
                "merged_id": "uv-c",
                "merged_content": "包管理与依赖管理都用 uv。",
                "merged_detail": None,
                "reason": "同一事实重复沉淀",
            }
        return {"verdict": "UNRELATED", "reason": "无关"}


@pytest.fixture
def env(tmp_path, fake_embedder, entry_factory):
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    settings = Settings(data_dir=tmp_path)
    for eid, content in [("uv-a", "包管理器用 uv。"), ("uv-b", "依赖管理用 uv。")]:
        entry = entry_factory(entry_id=eid, content=content)
        store.create(entry)
        index.upsert(entry, fake_embedder.embed_texts([entry.index_text])[0])
    index.close()
    return tmp_path, settings, fake_embedder


def _run(env, llm, dry_run=False):
    tmp_path, settings, embedder = env
    return run_evolution_cycle(settings, llm, embedder=embedder, dry_run=dry_run, now=NOW)


class TestTrigger:
    def test_not_triggered_when_recent_and_quiet(self, env):
        tmp_path, settings, _ = env
        # last_run 设为 NOW：两条记忆 created_at 不晚于它 → 新增 0；无积压；间隔 0 天
        save_last_run_at(tmp_path, NOW)
        report = _run(env, CycleLLM())
        assert not report.triggered
        assert report.proposal is None

    def test_state_roundtrip(self, env):
        tmp_path, _, _ = env
        assert load_last_run_at(tmp_path) is None
        save_last_run_at(tmp_path, NOW)
        assert load_last_run_at(tmp_path) == NOW


class TestDryRun:
    def test_dry_run_stops_at_proposal(self, env):
        tmp_path, _, _ = env
        report = _run(env, CycleLLM(), dry_run=True)

        assert report.triggered and report.dry_run
        assert report.proposal is not None
        assert [c.kind for c in report.proposal.changes] == ["merge"]
        # 提案已落盘，但记忆层、审计、状态都没动
        assert (report.proposal_dir / "proposal.yaml").exists()
        store = MarkdownStore(tmp_path)
        assert {e.id for e in store.list()} == {"uv-a", "uv-b"}
        assert not (tmp_path / "logs" / "evolution_audit.jsonl").exists()
        assert load_last_run_at(tmp_path) is None


class TestFullCycle:
    def test_verify_pass_then_applies(self, env):
        tmp_path, _, _ = env
        report = _run(env, CycleLLM(boundary_pass=True))

        assert report.applied
        assert report.verify.passed
        store = MarkdownStore(tmp_path)
        assert {e.id for e in store.list()} == {"uv-c"}
        # 审计 + 快照 + 状态
        audit = (tmp_path / "logs" / "evolution_audit.jsonl").read_text(encoding="utf-8")
        assert "evolve-20260820" in audit
        assert (tmp_path / "snapshots" / report.apply.snapshot_id / "memory").exists()
        assert load_last_run_at(tmp_path) == NOW
        # 验证结果与提案同目录留档
        verdict = json.loads((report.proposal_dir / "verdict.json").read_text(encoding="utf-8"))
        assert verdict["passed"] is True

    def test_verify_veto_archives_without_applying(self, env):
        tmp_path, _, _ = env
        report = _run(env, CycleLLM(boundary_pass=False))

        assert report.triggered and not report.applied
        assert not report.verify.passed
        # 记忆层不动；提案 + 验证结果留在 review_queue/evolution/ 交人工
        store = MarkdownStore(tmp_path)
        assert {e.id for e in store.list()} == {"uv-a", "uv-b"}
        verdict = json.loads((report.proposal_dir / "verdict.json").read_text(encoding="utf-8"))
        assert verdict["passed"] is False
        assert verdict["boundary"]["passed"] is False
        assert not (tmp_path / "logs" / "evolution_audit.jsonl").exists()
        assert load_last_run_at(tmp_path) is None  # 未晋升不刷新水位，下轮还会触发

    def test_boundary_llm_error_also_vetoes(self, env):
        report = _run(env, CycleLLM(boundary_pass=None))
        assert not report.applied
        assert not report.verify.boundary.passed
