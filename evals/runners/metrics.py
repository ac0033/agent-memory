"""配对统计与进化指标（M4b）：有记忆 vs 空库 baseline 的逐题配对比较。

书中方法论：两组必须共享任务与随机种子才是配对——同一批用例、同一份灌库
会话与判定 rubric，唯一自变量是"库里有没有记忆"。逐题记录谁胜出（discordant
对），用 McNemar 精确检验（二项检验）给 p 值，用配对 bootstrap 给增益的
置信区间。样本小（默认 < 20 条用例）时这些数字只作方向性参考，
`PairedStats.small_sample` 会显式标记，不得据此下强结论。

三个进化指标（埋点统计，由 e2e_eval 采集、本模块汇总）：
- 激活率：写入的记忆被召回（retrieval_count > 0）的比例；
- 遵循率：评委判定"PASS 所依据的事实实际来自召回记忆"的用例比例；
- 留出增益：有记忆组准确率 - baseline 组准确率（配对 bootstrap 给区间）。

全部为纯函数 / 纯数据，不依赖 numpy/scipy，方便单测。
"""

import math
import random
from dataclasses import dataclass

# 样本量门槛：低于该值时配对统计只作方向性参考
SMALL_SAMPLE_MIN = 20

# 配对 bootstrap 默认重采样次数
DEFAULT_BOOTSTRAP = 2000


def mcnemar_exact_p(discordant_memory_wins: int, discordant_baseline_wins: int) -> float:
    """McNemar 精确检验（双侧）：discordant 对 b（仅有记忆组过）与 c（仅 baseline 过）。

    原假设：两组无差异，discordant 对的归属服从 p=0.5 的二项分布。
    双侧 p = 2 * P(X <= min(b, c))，X ~ Binomial(b+c, 0.5)，封顶 1.0。
    b == c == 0（没有 discordant 对）时返回 1.0。
    """
    b, c = discordant_memory_wins, discordant_baseline_wins
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def paired_bootstrap_gain(
    with_memory: list[bool],
    baseline: list[bool],
    *,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = 42,
) -> tuple[float, float, float]:
    """配对 bootstrap：返回 (观测增益, 95% CI 下界, 上界)。

    逐题差值 d_i = with_memory[i] - baseline[i]（∈ {-1, 0, 1}），
    对用例做有放回重采样（两组同下标一起抽，保持配对），
    取重采样均值分布的 2.5% / 97.5% 分位。seed 固定，结果可复现。
    """
    if len(with_memory) != len(baseline):
        raise ValueError("两组必须是同一批用例的配对结果（等长）")
    n = len(with_memory)
    if n == 0:
        raise ValueError("配对结果不能为空")
    diffs = [int(w) - int(b) for w, b in zip(with_memory, baseline, strict=True)]
    observed = sum(diffs) / n
    rng = random.Random(seed)
    boot_means = []
    for _ in range(n_boot):
        total = sum(diffs[rng.randrange(n)] for _ in range(n))
        boot_means.append(total / n)
    boot_means.sort()
    lo = boot_means[max(0, int(0.025 * n_boot))]
    hi = boot_means[min(n_boot - 1, int(0.975 * n_boot))]
    return observed, lo, hi


@dataclass
class PairedStats:
    """有记忆组 vs baseline 组的配对比较结果。"""

    n: int  # 配对用例数
    both_pass: int  # 两组都通过
    both_fail: int  # 两组都失败
    memory_only: int  # 仅有记忆组通过（McNemar 的 b）
    baseline_only: int  # 仅 baseline 通过（McNemar 的 c）
    mcnemar_p: float
    gain: float  # 留出增益：有记忆组准确率 - baseline 组准确率
    gain_ci_lo: float
    gain_ci_hi: float

    @property
    def small_sample(self) -> bool:
        """样本不足以下强结论（用例数少于门槛）。"""
        return self.n < SMALL_SAMPLE_MIN


def paired_stats(
    with_memory: list[bool],
    baseline: list[bool],
    *,
    n_boot: int = DEFAULT_BOOTSTRAP,
    seed: int = 42,
) -> PairedStats:
    """汇总两组配对结果：逐题胜负计数 + McNemar p + bootstrap 增益区间。"""
    if len(with_memory) != len(baseline):
        raise ValueError("两组必须是同一批用例的配对结果（等长）")
    both_pass = sum(1 for w, b in zip(with_memory, baseline, strict=True) if w and b)
    both_fail = sum(1 for w, b in zip(with_memory, baseline, strict=True) if not w and not b)
    memory_only = sum(1 for w, b in zip(with_memory, baseline, strict=True) if w and not b)
    baseline_only = sum(1 for w, b in zip(with_memory, baseline, strict=True) if b and not w)
    gain, lo, hi = paired_bootstrap_gain(with_memory, baseline, n_boot=n_boot, seed=seed)
    return PairedStats(
        n=len(with_memory),
        both_pass=both_pass,
        both_fail=both_fail,
        memory_only=memory_only,
        baseline_only=baseline_only,
        mcnemar_p=mcnemar_exact_p(memory_only, baseline_only),
        gain=gain,
        gain_ci_lo=lo,
        gain_ci_hi=hi,
    )


def mean_or_none(values: list[float | None]) -> float | None:
    """忽略 None 取均值；全 None 或空列表返回 None（指标 N/A 而非 0）。"""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None
