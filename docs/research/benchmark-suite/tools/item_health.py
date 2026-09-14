"""用例健康检查：从评测运行结果里找可能有问题的用例，输出核验台的自动体检提示（flags.json）。

规则（只作提示，交给人工核验，不自动改用例）：
1. oracle（只给证据消息）没通过 → 金标、题面或证据标注可能有问题；
2. 同一模式下至少 3 个系统且全部没通过（含 full_context / oracle 时尤其可疑）→ 可能过难或有歧义；
3. no_memory 通过，且用例需要记忆（排除"不应浮现"类负例）→ 可能不靠记忆就能答，区分度不足。

用法（仓库根目录）：
  .venv/Scripts/python.exe docs/research/benchmark-suite/tools/item_health.py \
      data/logs/memcompass/t2-base data/logs/memcompass/t2-v2 data/logs/memcompass/t2-ctrl --out flags.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runners"))
from mc_common import SUBSET_ALIAS, load_items, utf8_stdout  # noqa: E402
from mc_report import SHORT, load, outcome  # noqa: E402

MODE = {"S": "系统层", "E": "端到端", "Q": "问答"}


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("flags.json"))
    args = ap.parse_args()

    rows, _ = load(args.runs)
    rows = [r for r in rows if not r.get("error")]
    items = {it["id"]: it for it in load_items(list(SUBSET_ALIAS))}
    by: dict[str, dict[str, dict[str, bool]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        o = outcome(r, r["subset"], r["mode"])
        if o is not None:
            by[r["item_id"]][r["mode"]][r["system"]] = bool(o)

    flags: dict[str, list[str]] = defaultdict(list)
    rule_count: Counter = Counter()
    for iid, modes in sorted(by.items()):
        it = items.get(iid) or {}
        negative = str(it.get("type", "")).startswith("should_not")
        for mode, res in sorted(modes.items()):
            m = MODE.get(mode, mode)
            if "oracle" in res and not res["oracle"]:
                flags[iid].append(f"{m}模式：oracle（直接给证据消息）也没通过——金标、题面或证据标注可能有问题")
                rule_count[(SHORT.get(it.get("subset"), "?"), "oracle 失败")] += 1
            if len(res) >= 3 and not any(res.values()):
                flags[iid].append(f"{m}模式：全部 {len(res)} 个系统都没通过（{'、'.join(sorted(res))}）——可能过难或有歧义")
                rule_count[(SHORT.get(it.get("subset"), "?"), "全部失败")] += 1
            if res.get("no_memory") and not negative:
                flags[iid].append(f"{m}模式：no_memory（不给任何记忆）也通过了——可能不靠记忆就能答，区分度不足")
                rule_count[(SHORT.get(it.get("subset"), "?"), "无记忆也通过")] += 1

    args.out.write_text(json.dumps(flags, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"检查 {len(by)} 条用例，{len(flags)} 条有提示 → {args.out}")
    for (sub, rule), n in sorted(rule_count.items()):
        print(f"  {sub:<3} {rule}：{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
