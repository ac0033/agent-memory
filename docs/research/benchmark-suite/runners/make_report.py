"""把各次运行汇成最终报告：test 切分为主（运行含其他切分时可放 {{ALL_SPLITS}} 附录），另附消融与评委一致性。

用法（仓库根目录）：
  .venv/Scripts/python.exe docs/research/benchmark-suite/runners/make_report.py \
      --base data/logs/memcompass/t2-base --v2 data/logs/memcompass/t2-v2 \
      --controls data/logs/memcompass/t2-ctrl-a data/logs/memcompass/t2-ctrl-b \
      --ablation data/logs/memcompass/t2-abl-no-surface data/logs/memcompass/t2-abl-no-archive \
      --skeleton <骨架.md> --conclusions <结论.md> --cases <案例.md> --out <报告.md>

骨架里的 {{PROFILE_TEST}} / {{SUBSETS_TEST}} / {{ALL_SPLITS}} / {{ABLATION}} / {{COST}} / {{JUDGE_AGREEMENT}} /
{{CONCLUSIONS}} / {{CASES}} 会被替换（骨架里没有的占位符直接忽略）；结论与案例由人工撰写（读完数字再写）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def report(runs: list[Path], ref: str, splits: str | None, out: Path) -> str:
    cmd = [PY, str(HERE / "mc_report.py"), *map(str, runs), "--ref", ref, "--out", str(out)]
    if splits:
        cmd += ["--splits", splits]
    subprocess.run(cmd, check=True, capture_output=True)
    return out.read_text(encoding="utf-8")


def body(md: str) -> tuple[str, str, str]:
    """拆出"分子集""能力画像""成本"三部分（去掉报告自带的标题、运行信息与出错清单）。"""
    lines = md.split("\n")
    start = next(i for i, x in enumerate(lines) if x.startswith("## "))
    text = "\n".join(lines[start:]).split("## 出错任务")[0]
    cost = ""
    if "## 成本" in text:
        text, cost = text.split("## 成本", 1)
        cost = "\n".join(cost.split("\n")[1:]).strip()
    if "## 能力画像" in text:
        subsets, profile = text.split("## 能力画像", 1)
        profile = "\n".join(profile.split("\n")[1:])
        return subsets.replace("## ", "### "), profile.strip(), cost
    return text, "", cost


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, required=True)
    ap.add_argument("--v2", type=Path, required=True)
    ap.add_argument("--controls", type=Path, nargs="*", default=[],
                    help="对照组运行（no_memory、oracle、naive_rag 等），与基线、优化版合并后一起与 am_base 配对比较")
    ap.add_argument("--ablation", type=Path, nargs="*", default=[])
    ap.add_argument("--judge-agreement", type=Path, default=None, help="judge_agreement.py 的输出文件")
    ap.add_argument("--skeleton", type=Path, required=True)
    ap.add_argument("--conclusions", type=Path, default=None)
    ap.add_argument("--cases", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mc-report-"))
    main_runs = [args.base, args.v2, *args.controls]
    test_sub, test_prof, test_cost = body(report(main_runs, "am_base", "test", tmp / "test.md"))
    all_sub, all_prof, _ = body(report(main_runs, "am_base", None, tmp / "all.md"))
    abl = []
    for d in args.ablation:
        import json
        import re

        sub, _, _ = body(report([args.v2, d], "am_v2", "test", tmp / f"{d.name}.md"))
        # 只保留该消融运行覆盖的子集（v2 全量运行里的其他子集与消融无关）
        keep = set(json.loads((d / "meta.json").read_text(encoding="utf-8")).get("subsets") or [])
        parts = re.split(r"(?m)^(?=### )", sub)
        sub = "".join(p for p in parts if not p.startswith("### ") or p.split()[1] in keep)
        abl.append(f"#### {d.name}（与 am_v2 在 test 切分上配对）\n\n{sub}")
    text = args.skeleton.read_text(encoding="utf-8")
    repl = {
        "{{PROFILE_TEST}}": test_prof or "（无）",
        "{{SUBSETS_TEST}}": test_sub,
        "{{ALL_SPLITS}}": f"#### 能力画像\n\n{all_prof}\n\n{all_sub}",
        "{{ABLATION}}": "\n\n".join(abl) or "（未运行）",
        "{{COST}}": test_cost or "（运行结果里没有成本字段）",
        "{{JUDGE_AGREEMENT}}": args.judge_agreement.read_text(encoding="utf-8").replace("# ", "#### ", 1)
        if args.judge_agreement and args.judge_agreement.exists() else "（未运行）",
        "{{CONCLUSIONS}}": args.conclusions.read_text(encoding="utf-8") if args.conclusions else "（待撰写）",
        "{{CASES}}": args.cases.read_text(encoding="utf-8") if args.cases else "（待撰写）",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    args.out.write_text(text, encoding="utf-8")
    print(f"报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
