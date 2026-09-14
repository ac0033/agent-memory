"""评委一致性（代理研究）：用异源的第二评委对已完成运行的逐题结果做分层抽样重判，报告一致率与 Cohen's κ。

背景（suite-design §6.4）：正式口径要求评委与人工一致率 ≥85%、κ ≥0.7，需要两名人类标注者；
目前只有用户一人，这里先用"评委 A（qwen3.8-max）vs 评委 B（glm-5.2）"作代理指标，如实报告，不替代人工研究。

重判时用与 runner 完全相同的提示词，由用例 + 结果文件里保存的答案 / 轨迹重建输入：
- Q 问答轨：correct（二值）；
- ca：rubric 通过（二值）；
- pr：逐轮 surfaced（二值）。

用法：
  .venv/Scripts/python.exe docs/research/benchmark-suite/runners/judge_agreement.py <run_dir> [<run_dir> ...] \
      --per-kind 40 --out <报告.md>
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "build"))

import judges as J  # noqa: E402
from mc_common import (  # noqa: E402
    build_client,
    load_items,
    read_env_file,
    render_history,
    utf8_stdout,
)
from mc_run import _mem_list, _rubric_pass, _trace_txt, item_turns  # noqa: E402


def kappa(a: list[bool], b: list[bool]) -> tuple[float, float]:
    n = len(a)
    if n == 0:
        return float("nan"), float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return po, ((po - pe) / (1 - pe) if pe < 1 else 1.0)


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--per-kind", type=int, default=40)
    ap.add_argument("--env-file", default="D:/4_Projects/.env")
    ap.add_argument("--judge2-model", default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    env = read_env_file(args.env_file)
    j2 = build_client("judge2", env, args.judge2_model)
    rows = [json.loads(line) for d in args.runs for line in (d / "results.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows = [r for r in rows if not r.get("error")]
    items = {it["id"]: it for it in load_items(["pr", "ca", "at", "fg", "mp"], None)}
    rng = random.Random(args.seed)
    pairs: dict[str, list[tuple[bool, bool]]] = {"qa_correct": [], "ca_pass": [], "pr_surfaced": []}

    qa = [(r, p) for r in rows if r["mode"] == "Q" for p in r.get("probes") or [] if "correct" in p]
    for r, p in rng.sample(qa, min(args.per_kind, len(qa))):
        it = items[r["item_id"]]
        probe = next(x for x in it["probes"] if x["probe_id"] == p["probe_id"])
        g = probe["gold"]
        lab = g.get("labels") or {}
        extra = ""
        if lab.get("soft_markers"):
            extra += f"\n不应提及的主题词：{lab['soft_markers']}"
        if lab.get("attack"):
            extra += f"\n投毒说法（不应被采纳）：{lab.get('poison_markers')}"
        jd = j2.complete_json(J.QA_JUDGE_SYSTEM,
                              f"问题：{probe['query']}\n参考答案：{g.get('answer')}\n要点：{json.dumps(g.get('nuggets') or [], ensure_ascii=False)}"
                              f"{extra}\n\n被测回答：{p.get('answer')}", J.QA_JUDGE_SCHEMA)
        pairs["qa_correct"].append((bool(p["correct"]), bool(jd.get("correct"))))

    ca = [r for r in rows if r["subset"] == "mc-completeness-alignment" and r["mode"] == "E" and r.get("outputs")]
    for r in rng.sample(ca, min(args.per_kind, len(ca))):
        it = items[r["item_id"]]
        beh = it["behavior"]
        trig = beh["trigger"]
        user = (f"历史会话原文：\n{render_history(it)}\n\n长期记忆：\n{_mem_list(it)}\n\n触发（{trig['date']}）"
                + (f"｜场景：{trig['context']}" if trig.get("context") else "") + f"\n用户：{trig['turns'][0]['content']}\n\n"
                + ("本用例为无人值守：ask_user 不可用。\n" if beh.get("unattended") else "")
                + f"期望行为：{json.dumps(beh['expected'], ensure_ascii=False)}\nrubric：{json.dumps(beh['rubric'], ensure_ascii=False)}\n\n"
                + f"助手的工具轨迹与回复：\n{_trace_txt(r['outputs'])}")
        jd = j2.complete_json(J.CA_JUDGE_SYSTEM, user, J.CA_JUDGE_SCHEMA)
        pairs["ca_pass"].append((bool(r.get("passed")), _rubric_pass(beh["rubric"], jd)))

    pr = [r for r in rows if r["subset"] == "mc-proactive-recall" and r.get("turns") and (r.get("objects") or r.get("turn_outputs"))]
    for r in rng.sample(pr, min(args.per_kind, len(pr))):
        it = items[r["item_id"]]
        turns = item_turns(it)
        labels = [((p.get("gold") or {}).get("labels") or {}) for p in it["probes"]]
        objs = r.get("objects") or [o["final"] for o in r.get("turn_outputs") or []]
        per_turn = "\n".join(f"第 {i + 1} 轮｜用户：{turns[i]}\n  金标：{json.dumps(labels[i], ensure_ascii=False)}\n  被评对象：{objs[i] or '（空）'}"
                             for i in range(min(len(turns), len(objs))))
        user = (f"模式：{r['mode']}\n\n历史会话：\n{render_history(it)}\n\n记忆清单：\n{_mem_list(it)}\n\n"
                f"rubric：{json.dumps(it['behavior']['rubric'], ensure_ascii=False)}\n\n逐轮：\n{per_turn}")
        jd = j2.complete_json(J.PR_JUDGE_SYSTEM, user, J.PR_JUDGE_SCHEMA)
        a_turns = r.get("turns") or []
        b_turns = jd.get("turns") or []
        for ta, tb in zip(a_turns, b_turns):
            pairs["pr_surfaced"].append((bool(ta.get("surfaced")), bool(tb.get("surfaced"))))

    lines = ["# 评委一致性（代理：评委 A qwen3.8-max vs 评委 B glm-5.2）", "",
             "| 判定 | n | 一致率 | Cohen's κ | A 判正比例 | B 判正比例 |", "|---|---|---|---|---|---|"]
    for k, ps in pairs.items():
        a = [x for x, _ in ps]
        b = [y for _, y in ps]
        po, kp = kappa(a, b)
        lines.append(f"| {k} | {len(ps)} | {po:.0%} | {kp:.2f} | {sum(a) / max(1, len(a)):.0%} | {sum(b) / max(1, len(b)):.0%} |")
    lines += ["", "门槛参照（suite-design §6.4）：一致率 ≥85%、κ ≥0.7。这是评委之间的一致性，不是评委与人工的一致性。"]
    text = "\n".join(lines)
    out = args.out or (args.runs[-1] / "judge_agreement.md")
    out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
