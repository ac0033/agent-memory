"""验收运行的配对报告：按题型分桶的正确率 + 两两 McNemar 精确检验。

每一列写成 `run_id:system`，可以跨 run 取列（例如基线在旧 run 里、被测系统在新 run 里）：

    uv run python docs/research/benchmark-suite/runners/paired_report.py \
        v04-n60b:naive_rag v04-n60b:am v1-final-n60:am

只统计所有列都有结果的题（配对）。最后一列视为被测系统，与前面每一列各做一次配对检验。
"""

from __future__ import annotations

import collections
import io
import json
import sys
from math import comb
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
ROOT = REPO / "data" / "logs" / "aml_selftest"


def load(spec: str) -> dict[str, dict]:
    run, system = spec.split(":")
    rows: dict[str, dict] = {}
    for line in (ROOT / run / "results.jsonl").open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("system") == system and r.get("passed") is not None and not r.get("error"):
            rows[str(r["question_id"])] = r
    return rows


def mcnemar(b: int, c: int) -> float:
    """双侧精确 McNemar：b、c 为两个方向的不一致对数。"""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(min(b, c) + 1)) / 2**n)


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    specs = [a for a in sys.argv[1:] if not a.startswith("-")]
    cols = [(s, load(s)) for s in specs]
    ids = sorted(set.intersection(*(set(r) for _, r in cols)))
    print(f"配对题数 {len(ids)}；各列完成数 " + ", ".join(f"{s}={len(r)}" for s, r in cols))
    buckets: dict[str, list[str]] = collections.OrderedDict()
    for q in ids:
        buckets.setdefault(cols[-1][1][q]["question_type"], []).append(q)
    width = max(len(b) for b in [*buckets, "TOTAL"]) + 2
    print(f"{'题型':{width}s} n   " + "  ".join(f"{s:>22s}" for s, _ in cols))
    for b, qs in [*sorted(buckets.items()), ("TOTAL", ids)]:
        print(f"{b:{width}s} {len(qs):<3d} " + "  ".join(f"{sum(bool(r[q]['passed']) for q in qs):>22d}" for _, r in cols))
    n = max(len(ids), 1)
    print(f"{'平均提示字符':{width}s} -   " + "  ".join(f"{sum(r[q].get('prompt_chars') or 0 for q in ids) // n:>22,d}" for _, r in cols))
    name, target = cols[-1]
    for other_name, other in cols[:-1]:
        win = [q for q in ids if target[q]["passed"] and not other[q]["passed"]]
        lose = [q for q in ids if not target[q]["passed"] and other[q]["passed"]]
        print(f"\n{name} vs {other_name}: 独赢 {len(win)} / 独输 {len(lose)}，McNemar p={mcnemar(len(win), len(lose)):.3f}")
        if "-v" in sys.argv:
            for q in lose:
                print(f"  独输 {q} [{target[q]['question_type']}] gold={str(target[q]['gold'])[:60]!r} ans={target[q]['answer'][:120]!r}")


if __name__ == "__main__":
    main()
