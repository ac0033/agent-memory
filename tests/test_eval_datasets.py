"""evals/datasets 评估用例的结构校验。

评估集属于可信根（D6）：这个测试保证用例始终可解析、结构完整，
任何 agent 不得绕过此测试修改 evals/。
layer1：20 条基础回忆用例（单/双 session）；
layer2：20 条多会话检索/消歧用例（sessions ≥ 2，跨时间，含时序冲突与多对象消歧）；
layer3：12 条跨会话隐藏关联用例（答对需要"常驻 profile 概览 + 按需检索细节"
双层配合，rubric.essential 必须含"主动提示隐藏关联"项）。
"""

from pathlib import Path

import pytest
import yaml

DATASET_DIR = Path(__file__).parent.parent / "evals" / "datasets" / "layer1"
DATASET_DIR_L2 = Path(__file__).parent.parent / "evals" / "datasets" / "layer2"
DATASET_DIR_L3 = Path(__file__).parent.parent / "evals" / "datasets" / "layer3"

REQUIRED_TOP_KEYS = {
    "id",
    "layer",
    "description",
    "sessions",
    "question",
    "reference_answer",
    "rubric",
    "memories_expected",
}
VALID_MEMORY_TYPES = {"semantic", "procedural", "episodic", "profile"}


def load_all(dataset_dir: Path) -> dict[str, dict]:
    cases = {}
    for f in sorted(dataset_dir.glob("*.yaml")):
        with open(f, encoding="utf-8") as fh:
            cases[f.name] = yaml.safe_load(fh)
    return cases


CASES = load_all(DATASET_DIR)
CASES_L2 = load_all(DATASET_DIR_L2)
CASES_L3 = load_all(DATASET_DIR_L3)


def test_exactly_20_cases_exist():
    assert len(CASES) == 20, f"期望 20 条用例，实际 {len(CASES)} 条"


def test_ids_unique_and_layer1():
    ids = [c["id"] for c in CASES.values()]
    assert len(ids) == len(set(ids)), "用例 id 重复"
    for name, case in CASES.items():
        assert case["layer"] == 1, f"{name} 的 layer 不是 1"
        assert case["id"].startswith("layer1-"), f"{name} 的 id 前缀不对"


def _check_turns(sessions):
    for session in sessions:
        assert session["session_id"], "session_id 不能为空"
        turns = session["turns"]
        assert 2 <= len(turns) <= 6, "turns 应为 2-6 轮"
        for turn in turns:
            assert turn["role"] in {"user", "assistant"}
            assert turn["content"].strip(), "turn content 不能为空"


def _check_rubric_and_expected(case):
    rubric = case["rubric"]
    assert rubric["essential"], "rubric.essential 不能为空"
    assert rubric["pitfalls"], "rubric.pitfalls 至少 1 条"

    assert case["memories_expected"], "memories_expected 不能为空"
    for mem in case["memories_expected"]:
        assert mem["id"], "memories_expected 条目缺 id"
        assert mem["content"].strip(), "memories_expected 条目缺 content"
        assert mem["memory_type"] in VALID_MEMORY_TYPES


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_case_structure(case):
    assert REQUIRED_TOP_KEYS <= set(case), f"缺少字段: {REQUIRED_TOP_KEYS - set(case)}"

    sessions = case["sessions"]
    assert 1 <= len(sessions) <= 2, "sessions 应为 1-2 个"
    _check_turns(sessions)
    _check_rubric_and_expected(case)


def test_four_memory_types_covered():
    covered = {m["memory_type"] for c in CASES.values() for m in c["memories_expected"]}
    assert covered == VALID_MEMORY_TYPES, f"未覆盖全部记忆类型，缺: {VALID_MEMORY_TYPES - covered}"


# ---------------------------------------------------------------- layer2


def test_layer2_exactly_20_cases_exist():
    assert len(CASES_L2) == 20, f"layer2 期望 20 条用例，实际 {len(CASES_L2)} 条"


def test_layer2_ids_unique_and_layer2():
    ids = [c["id"] for c in CASES_L2.values()]
    assert len(ids) == len(set(ids)), "layer2 用例 id 重复"
    for name, case in CASES_L2.items():
        assert case["layer"] == 2, f"{name} 的 layer 不是 2"
        assert case["id"].startswith("layer2-"), f"{name} 的 id 前缀不对"


@pytest.mark.parametrize("case", CASES_L2.values(), ids=CASES_L2.keys())
def test_layer2_case_structure(case):
    assert REQUIRED_TOP_KEYS <= set(case), f"缺少字段: {REQUIRED_TOP_KEYS - set(case)}"

    sessions = case["sessions"]
    # layer2 是多会话用例：至少 2 个 session，至多 4 个
    assert 2 <= len(sessions) <= 4, "layer2 sessions 应为 2-4 个"
    session_ids = [s["session_id"] for s in sessions]
    assert len(session_ids) == len(set(session_ids)), "session_id 重复"
    _check_turns(sessions)
    _check_rubric_and_expected(case)

    # per-session memories（mock 灌库 oracle）：结构合法、supersedes 指向已出现的 id
    seen_ids: set[str] = set()
    for session in sessions:
        mems = session.get("memories", [])
        assert isinstance(mems, list), f"{session['session_id']} 的 memories 必须是列表"
        for mem in mems:
            assert mem["id"], "memories 条目缺 id"
            assert mem["content"].strip(), "memories 条目缺 content"
            assert mem["memory_type"] in VALID_MEMORY_TYPES
            if "supersedes" in mem:
                assert mem["supersedes"] in seen_ids, (
                    f"{mem['id']} 的 supersedes 指向未先入库的 id: {mem['supersedes']}"
                )
            seen_ids.add(mem["id"])

    # memories_expected 必须是灌完所有 session 后仍存在的条目（supersedes 链末端）
    superseded = {
        m["supersedes"] for s in sessions for m in s.get("memories", []) if "supersedes" in m
    }
    final_ids = seen_ids - superseded
    for mem in case["memories_expected"]:
        assert mem["id"] in final_ids, (
            f"memories_expected 的 {mem['id']} 不在最终存活条目集合内"
        )


def test_layer2_category_coverage():
    """至少 6 条时序冲突、至少 6 条多对象消歧。"""
    temporal = sum(1 for c in CASES_L2.values() if c.get("category") == "temporal")
    disambiguation = sum(1 for c in CASES_L2.values() if c.get("category") == "disambiguation")
    assert temporal >= 6, f"时序冲突用例不足 6 条（实际 {temporal}）"
    assert disambiguation >= 6, f"多对象消歧用例不足 6 条（实际 {disambiguation}）"


def test_layer2_supersedes_chains_exist():
    """时序冲突用例必须体现 UPDATE/supersedes 语义。"""
    for name, case in CASES_L2.items():
        if case.get("category") != "temporal":
            continue
        has_supersedes = any(
            "supersedes" in m for s in case["sessions"] for m in s.get("memories", [])
        )
        assert has_supersedes, f"{name} 是时序冲突用例但没有 supersedes 链"


# ---------------------------------------------------------------- layer3


def test_layer3_exactly_12_cases_exist():
    assert len(CASES_L3) == 12, f"layer3 期望 12 条用例，实际 {len(CASES_L3)} 条"


def test_layer3_ids_unique_and_layer3():
    ids = [c["id"] for c in CASES_L3.values()]
    assert len(ids) == len(set(ids)), "layer3 用例 id 重复"
    for name, case in CASES_L3.items():
        assert case["layer"] == 3, f"{name} 的 layer 不是 3"
        assert case["id"].startswith("layer3-"), f"{name} 的 id 前缀不对"


@pytest.mark.parametrize("case", CASES_L3.values(), ids=CASES_L3.keys())
def test_layer3_case_structure(case):
    assert REQUIRED_TOP_KEYS <= set(case), f"缺少字段: {REQUIRED_TOP_KEYS - set(case)}"
    assert case.get("category") == "hidden_association", (
        "layer3 用例 category 应为 hidden_association"
    )

    sessions = case["sessions"]
    # layer3 是跨会话隐藏关联用例：至少 2 个 session（事实与计划分处不同会话）
    assert 2 <= len(sessions) <= 4, "layer3 sessions 应为 2-4 个"
    session_ids = [s["session_id"] for s in sessions]
    assert len(session_ids) == len(set(session_ids)), "session_id 重复"
    _check_turns(sessions)
    _check_rubric_and_expected(case)

    # rubric.essential 必须含"主动提示隐藏关联"项——layer3 评的就是主动服务
    assert any("主动提示" in e for e in case["rubric"]["essential"]), (
        "layer3 的 rubric.essential 必须包含一条'主动提示隐藏关联'项"
    )

    # 双层配合：per-session memories 里至少一条 profile（常驻层），
    # 且至少一条非 profile（检索层细节），缺一层就不是"概览+细节"的题
    all_mems = [m for s in sessions for m in s.get("memories", [])]
    types = {m["memory_type"] for m in all_mems}
    assert "profile" in types, "layer3 用例必须至少含一条 profile 记忆（常驻层）"
    assert types - {"profile"}, "layer3 用例必须至少含一条非 profile 记忆（检索层细节）"

    # per-session memories（mock 灌库 oracle）：结构合法、supersedes 指向已出现的 id
    seen_ids: set[str] = set()
    for session in sessions:
        mems = session.get("memories", [])
        assert isinstance(mems, list), f"{session['session_id']} 的 memories 必须是列表"
        for mem in mems:
            assert mem["id"], "memories 条目缺 id"
            assert mem["content"].strip(), "memories 条目缺 content"
            assert mem["memory_type"] in VALID_MEMORY_TYPES
            if "supersedes" in mem:
                assert mem["supersedes"] in seen_ids, (
                    f"{mem['id']} 的 supersedes 指向未先入库的 id: {mem['supersedes']}"
                )
            seen_ids.add(mem["id"])

    # memories_expected 必须是灌完所有 session 后仍存在的条目（supersedes 链末端）
    superseded = {
        m["supersedes"] for s in sessions for m in s.get("memories", []) if "supersedes" in m
    }
    final_ids = seen_ids - superseded
    for mem in case["memories_expected"]:
        assert mem["id"] in final_ids, (
            f"memories_expected 的 {mem['id']} 不在最终存活条目集合内"
        )
