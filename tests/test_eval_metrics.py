"""evals/runners/metrics.py 配对统计的单测（纯函数，不依赖模型与 LLM）。"""

import importlib.util
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).parent.parent / "evals" / "runners" / "metrics.py"


@pytest.fixture(scope="module")
def metrics():
    spec = importlib.util.spec_from_file_location("eval_metrics", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- McNemar


def test_mcnemar_known_value(metrics):
    """b=8, c=1：p = 2 * (C(9,0)+C(9,1)) / 2^9 = 20/512。"""
    p = metrics.mcnemar_exact_p(8, 1)
    assert p == pytest.approx(20 / 512)


def test_mcnemar_no_discordant(metrics):
    """没有 discordant 对（两组完全一致）时 p = 1.0。"""
    assert metrics.mcnemar_exact_p(0, 0) == 1.0


def test_mcnemar_symmetric(metrics):
    assert metrics.mcnemar_exact_p(8, 1) == metrics.mcnemar_exact_p(1, 8)


def test_mcnemar_balanced_not_significant(metrics):
    """b=c 时不应显著。"""
    assert metrics.mcnemar_exact_p(5, 5) == 1.0


# ---------------------------------------------------------------- bootstrap


def test_bootstrap_perfect_gain(metrics):
    """全部用例仅有记忆组过：增益恒为 1，区间退化为 [1, 1]。"""
    gain, lo, hi = metrics.paired_bootstrap_gain([True] * 12, [False] * 12)
    assert gain == 1.0
    assert lo == hi == 1.0


def test_bootstrap_zero_gain(metrics):
    """两组完全一致：增益恒为 0。"""
    gain, lo, hi = metrics.paired_bootstrap_gain([True, False] * 6, [True, False] * 6)
    assert gain == 0.0
    assert lo == hi == 0.0


def test_bootstrap_deterministic_with_seed(metrics):
    a = metrics.paired_bootstrap_gain([True, False, True, False], [False, False, True, False])
    b = metrics.paired_bootstrap_gain([True, False, True, False], [False, False, True, False])
    assert a == b


def test_bootstrap_observed_gain(metrics):
    with_mem = [True, True, False, True]
    baseline = [False, True, False, False]
    gain, lo, hi = metrics.paired_bootstrap_gain(with_mem, baseline, n_boot=500)
    assert gain == pytest.approx(0.5)
    assert lo <= gain <= hi


def test_bootstrap_rejects_unpaired(metrics):
    with pytest.raises(ValueError):
        metrics.paired_bootstrap_gain([True], [True, False])
    with pytest.raises(ValueError):
        metrics.paired_bootstrap_gain([], [])


# ---------------------------------------------------------------- paired_stats


def test_paired_stats_counts(metrics):
    with_mem = [True, True, False, False, True]
    baseline = [True, False, False, True, False]
    stats = metrics.paired_stats(with_mem, baseline, n_boot=500)
    assert stats.n == 5
    assert stats.both_pass == 1
    assert stats.both_fail == 1
    assert stats.memory_only == 2
    assert stats.baseline_only == 1
    assert stats.gain == pytest.approx((3 - 2) / 5)
    assert stats.mcnemar_p == pytest.approx(
        metrics.mcnemar_exact_p(2, 1)
    )
    # 5 条用例 < SMALL_SAMPLE_MIN，必须标记样本不足
    assert stats.small_sample


def test_paired_stats_large_sample_not_flagged(metrics):
    stats = metrics.paired_stats([True] * 25, [False] * 25, n_boot=200)
    assert not stats.small_sample


def test_mean_or_none(metrics):
    assert metrics.mean_or_none([0.5, None, 1.0]) == pytest.approx(0.75)
    assert metrics.mean_or_none([None, None]) is None
    assert metrics.mean_or_none([]) is None
