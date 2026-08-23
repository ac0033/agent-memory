"""evals/datasets/prefix/ 轨迹前缀回归用例的结构校验。

评估集属于可信根（D6）：这个测试保证用例始终可解析、结构完整。
prefix 用例是冻结上下文（system + 已注入记忆块 + 用户最新消息）+
可接受/禁止动作集合，供 prefix_regression.py 发给真实 LLM 判定。
"""

from pathlib import Path

import pytest
import yaml

DATASET_DIR = Path(__file__).parent.parent / "evals" / "datasets" / "prefix"

REQUIRED_TOP_KEYS = {"id", "category", "description", "context", "acceptable_actions",
                     "forbidden_actions"}
REQUIRED_CONTEXT_KEYS = {"system", "recalled_block", "user_message"}
# 四类边界场景必须全部覆盖
BOUNDARY_CATEGORIES = {
    "conflict_override",  # 旧偏好 vs 当前指令
    "scope_leak",  # 跨 scope 泄漏
    "low_confidence",  # 低置信度推断
    "injection_resistance",  # 记忆内容抗注入
}


def load_all() -> dict[str, dict]:
    cases = {}
    for f in sorted(DATASET_DIR.glob("*.yaml")):
        cases[f.name] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return cases


CASES = load_all()


def test_case_count_in_range():
    assert 8 <= len(CASES) <= 10, f"prefix 用例应为 8-10 条，实际 {len(CASES)} 条"


def test_ids_unique_and_prefixed():
    ids = [c["id"] for c in CASES.values()]
    assert len(ids) == len(set(ids)), "用例 id 重复"
    for name, case in CASES.items():
        assert case["id"].startswith("prefix-"), f"{name} 的 id 前缀不对"


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_case_structure(case):
    assert REQUIRED_TOP_KEYS <= set(case), f"缺少字段: {REQUIRED_TOP_KEYS - set(case)}"

    ctx = case["context"]
    missing_ctx = REQUIRED_CONTEXT_KEYS - set(ctx)
    assert not missing_ctx, f"context 缺少字段: {missing_ctx}"
    for key in REQUIRED_CONTEXT_KEYS:
        assert isinstance(ctx[key], str) and ctx[key].strip(), f"context.{key} 不能为空"

    for key in ("acceptable_actions", "forbidden_actions"):
        actions = case[key]
        assert isinstance(actions, list) and actions, f"{key} 必须是非空列表"
        for a in actions:
            assert isinstance(a, str) and a.strip(), f"{key} 存在空条目"


def test_boundary_categories_covered():
    covered = {c["category"] for c in CASES.values()}
    missing = BOUNDARY_CATEGORIES - covered
    assert not missing, f"边界场景覆盖不全，缺: {missing}"
    # 每类边界场景至少 2 条（单条无法区分"护栏有效"和"运气好"）
    for cat in BOUNDARY_CATEGORIES:
        n = sum(1 for c in CASES.values() if c["category"] == cat)
        assert n >= 2, f"{cat} 场景只有 {n} 条，至少 2 条"


def test_recalled_block_looks_like_inject_output():
    """冻结的记忆块必须带护栏前缀（与 inject.py 的输出形态一致）。"""
    for name, case in CASES.items():
        block = case["context"]["recalled_block"]
        assert "<recalled_memories>" in block, f"{name} 的 recalled_block 不是注入块形态"
        assert "参考而非指令" in block, f"{name} 的 recalled_block 缺护栏前缀"


def test_injection_cases_actually_contain_injection():
    """抗注入用例的记忆块里必须真的藏了指令性文本，否则测不出东西。"""
    injection_markers = ["ignore", "忽略", "以后都要", "必须"]
    for name, case in CASES.items():
        if case["category"] != "injection_resistance":
            continue
        block = case["context"]["recalled_block"].lower()
        assert any(m in block for m in injection_markers), (
            f"{name} 声称是抗注入用例但 recalled_block 里没有指令性文本"
        )
