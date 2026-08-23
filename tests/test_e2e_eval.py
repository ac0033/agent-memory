"""e2e_eval.py 规则降级模式冒烟测试（FakeEmbedder，不依赖真实模型与 LLM key）。

验证：runner 能对 layer1 / layer2 用例跑完整流程（mock 灌库 → 检索 → 渲染 →
规则判定），报告结构完整；选两条 FakeEmbedder 友好的用例验证端到端可通过。
"""

import importlib.util
from pathlib import Path

import pytest

from agent_memory.config import Settings

RUNNER_PATH = (
    Path(__file__).parent.parent / "evals" / "runners" / "e2e_eval.py"
)


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("e2e_eval", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rule_degraded_smoke_layer1(e2e, fake_embedder, tmp_path):
    """layer1-06（端口 + SQLite）：FakeEmbedder 的"端口"关键词可命中。"""
    settings = Settings(data_dir=tmp_path)
    case_file = e2e.DATASETS_DIR / "layer1" / "06_dev_port_and_db.yaml"
    result = e2e.run_case(case_file, fake_embedder, settings, llm=None)
    assert result.write_ok, "expected memory id 应全部在库"
    assert result.recall_hit, "expected id 应出现在 top5"
    assert result.passed, f"规则判定应通过: {result.judge_detail}"


def test_rule_degraded_smoke_layer2_temporal(e2e, fake_embedder, tmp_path):
    """layer2-01（SQLite→PostgreSQL→SQLite）：验证 supersedes 链走完只剩终态。"""
    settings = Settings(data_dir=tmp_path)
    case_file = e2e.DATASETS_DIR / "layer2" / "01_db_sqlite_pg_sqlite.yaml"
    result = e2e.run_case(case_file, fake_embedder, settings, llm=None)
    assert result.write_ok, "终态条目 proj-db-choice-v3 应在库"
    assert result.passed, f"规则判定应通过: {result.judge_detail}"


def test_run_once_report_structure(e2e, fake_embedder, tmp_path):
    settings = Settings(data_dir=tmp_path)
    case_files = e2e.load_case_files([2])[:3]
    report = e2e.run_once(case_files, fake_embedder, settings, llm=None)
    assert report.mode == "rule-degraded"
    assert len(report.results) == 3
    accuracy = report.accuracy_by_layer()
    assert 2 in accuracy
    assert 0.0 <= accuracy[2] <= 1.0


def test_run_once_parallel_matches_serial(e2e, fake_embedder, tmp_path):
    """jobs=4 并发与串行结果一致：顺序保持、判定结果相同。"""
    settings = Settings(data_dir=tmp_path)
    case_files = e2e.load_case_files([2])[:4]
    serial = e2e.run_once(case_files, fake_embedder, settings, llm=None, jobs=1)
    parallel = e2e.run_once(case_files, fake_embedder, settings, llm=None, jobs=4)
    assert [r.case_id for r in parallel.results] == [r.case_id for r in serial.results]
    assert [r.passed for r in parallel.results] == [r.passed for r in serial.results]
