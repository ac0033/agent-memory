"""把 MemCompass 的冻结副本迁入 evals/memcompass/（D6 可信根）。

授权：用户 2026-09-14——评测集核验通过、并能真实准确地反映能力后迁入。
默认只打印计划（dry run）；加 --apply 才写入；目标目录已存在时拒绝，除非 --force。

迁入（逐字节复制，便于与编写源头 diff）：
- datasets/<子集>/{card.md, examples.yaml, migrated.yaml, generated.yaml} 与 datasets/verification.yaml；
- runners/*.py（统一 runner、对照组、评委提示词、汇总、评委一致性、报告拼装）；
- tools/validate.py、tools/item_health.py；
- 另写 README.md（冻结说明、来源提交、核验记录、用法）。
不迁入：构造脚本 build/、核验台 tools/review/、设计与调研文档——它们留在 docs/research/benchmark-suite/，是编写源头。
用例文件不改写：生成文件不手改，核验以 datasets/verification.yaml 为准。
"""

from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
REPO = next(p for p in HERE.parents if (p / "pyproject.toml").exists())
DST = REPO / "evals" / "memcompass"
SUBSETS = [
    "mc-proactive-recall", "mc-completeness-alignment", "mc-asof-temporal", "mc-forget-request",
    "mc-memory-poisoning", "mc-task-state", "mc-present-fidelity", "mc-cross-agent",
]
DATA_FILES = ("card.md", "examples.yaml", "migrated.yaml", "generated.yaml")
TOOLS = ("validate.py", "item_health.py")

README = """# MemCompass（冻结副本）

> 本目录是 `docs/research/benchmark-suite/` 在 {date} 的冻结副本（来源提交 `{sha}`，套件版本 {version}），
> 属于 D6 可信根：**agent 不得修改**；更新由用户执行（在编写源头改规格、重新生成、重新核验后再迁入）。

- 规模：8 个子集，{n_items} 条用例；切分 dev / test / held-out = 20 / 50 / 30（按 group 整体分配）。
- 人工核验：见 `datasets/verification.yaml`（用例文件本身不改写，核验以该文件为准）。
- 编写源头（构造脚本、设计文档、核验台）：`docs/research/benchmark-suite/`。

## 用法（仓库根目录）

```bash
# 校验数据（纯本地）
.venv/Scripts/python.exe evals/memcompass/tools/validate.py --quiet
# 跑一个被测版本（test 切分；每个进程约 3.5 GB 内存，16 GB 机器上一次只跑一个）
.venv/Scripts/python.exe evals/memcompass/runners/mc_run.py --subsets pr,ca,at,fg,mp,xa,ts,pf --splits test \\
    --systems am --am-root <代码根目录> --am-label <标签> --env-file D:/4_Projects/.env --jobs 8
# 汇总与配对比较
.venv/Scripts/python.exe evals/memcompass/runners/mc_report.py data/logs/memcompass/<run_a> data/logs/memcompass/<run_b> --ref <参照系统>
# 用例健康检查（oracle 失败 / 全部失败 / 不给记忆也通过）
.venv/Scripts/python.exe evals/memcompass/tools/item_health.py data/logs/memcompass/<run> ... --out flags.json
```

held-out 切分只用于里程碑评测，平时的开发与调参不要跑它。
"""


def plan() -> list[tuple[Path, Path]]:
    pairs = []
    for s in SUBSETS:
        for f in DATA_FILES:
            src = SRC / "datasets" / s / f
            if src.exists():
                pairs.append((src, DST / "datasets" / s / f))
    pairs.append((SRC / "datasets" / "verification.yaml", DST / "datasets" / "verification.yaml"))
    for src in sorted((SRC / "runners").glob("*.py")):
        pairs.append((src, DST / "runners" / src.name))
    for t in TOOLS:
        pairs.append((SRC / "tools" / t, DST / "tools" / t))
    return pairs


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只打印计划）")
    ap.add_argument("--force", action="store_true", help="目标目录已存在时覆盖")
    args = ap.parse_args()

    pairs = plan()
    missing = [s for s, _ in pairs if not s.exists()]
    if missing:
        print("缺少源文件：", *missing, sep="\n  ")
        return 1
    print(f"源：{SRC}\n目标：{DST}\n共 {len(pairs)} 个文件 + README.md")
    for s, d in pairs:
        print(f"  {s.relative_to(SRC)}  →  {d.relative_to(REPO)}")
    if not args.apply:
        print("（dry run：加 --apply 才写入）")
        return 0
    if DST.exists() and not args.force:
        print(f"目标已存在：{DST}（确认要覆盖请加 --force）")
        return 1

    sys.path.insert(0, str(SRC / "runners"))
    from mc_common import SUBSET_ALIAS, load_items  # noqa: E402

    for s, d in pairs:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    bad = [d for s, d in pairs if not filecmp.cmp(s, d, shallow=False)]
    if bad:
        print("复制后内容不一致：", *bad, sep="\n  ")
        return 1
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "?"
    items = load_items(list(SUBSET_ALIAS))
    version = items[0].get("meta", {}).get("suite_version", "?") if items else "?"
    (DST / "README.md").write_text(
        README.format(date=dt.date.today().isoformat(), sha=sha, version=version, n_items=len(items)),
        encoding="utf-8")
    print(f"已迁入 {len(pairs)} 个文件（逐字节核对一致）+ README.md → {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
