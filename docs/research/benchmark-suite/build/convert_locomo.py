"""把 LoCoMo（snap-research/locomo，CC BY-NC 4.0，仅研究用途）转成 aml_selftest.py 能直接跑的形状。

LoCoMo 是两个人之间的多会话对话，不是"用户-助手"——正好检验机制在另一种对话形态上的泛化。
映射：speaker_a → role=user，speaker_b → role=assistant，每条消息正文前缀说话人名字
（AML 的 Add 契约只允许 user / assistant 两种 role）；图片轮次把 blip_caption 写进正文。

同一段对话的所有题共享一份历史（runner 会复用已建好的库）。按类别分层抽样：
1 多跳、2 时间、3 开放域、4 单跳、5 对抗（前提不成立，金标为"未提及"）。

数据不入库（data/ 已 gitignore）。外部集原则上只测一次：这个脚本只负责转换，不负责调参。

    uv run python docs/research/benchmark-suite/build/convert_locomo.py --conversations 0,1 --per-category 6
    → data/external/locomo/sample.json（验收用的两份样本已存为 sample_a.json / sample_b.json）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SRC = REPO / "data" / "external" / "locomo" / "locomo10.json"
OUT = REPO / "data" / "external" / "locomo" / "sample.json"
CATEGORY = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def parse_when(text: str) -> dt.datetime:
    """'1:56 pm on 8 May, 2023' -> datetime。"""
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm) on (\d{1,2}) (\w+),? (\d{4})", text.strip(), re.I)
    if not m:
        raise ValueError(f"无法解析会话时间: {text!r}")
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    month = dt.datetime.strptime(m.group(5)[:3], "%b").month
    return dt.datetime(int(m.group(6)), month, int(m.group(4)), hour, int(m.group(2)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", default="0,1", help="取哪几段对话（下标，逗号分隔）")
    ap.add_argument("--per-category", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    data = json.loads(SRC.read_text(encoding="utf-8"))
    rnd = random.Random(args.seed)
    out = []
    for ci in (int(x) for x in args.conversations.split(",")):
        sample = data[ci]
        conv = sample["conversation"]
        a = conv["speaker_a"]
        keys = sorted((k for k in conv if re.fullmatch(r"session_\d+", k)), key=lambda k: int(k.split("_")[1]))
        ids, dates, sessions, last = [], [], [], None
        for k in keys:
            when = parse_when(conv[f"{k}_date_time"])
            last = when
            msgs = []
            for t in conv[k]:
                text = t.get("text", "").strip()
                if t.get("blip_caption"):
                    text += f" [shares a photo: {t['blip_caption']}]"
                if text:
                    role = "user" if t["speaker"] == a else "assistant"
                    msgs.append({"role": role, "content": f"{t['speaker']}: {text}"})
            ids.append(f"{sample['sample_id']}-{k}")
            dates.append(when.strftime("%Y/%m/%d (%a) %H:%M"))
            sessions.append(msgs)
        by_cat: dict[int, list[dict]] = {}
        for q in sample["qa"]:
            by_cat.setdefault(q["category"], []).append(q)
        for cat in sorted(by_cat):
            pool = by_cat[cat][:]
            rnd.shuffle(pool)
            for j, q in enumerate(pool[: args.per_category]):
                gold = q.get("answer")
                if cat == 5:
                    gold = ("The information provided is not enough: this was never mentioned in the "
                            f"conversation. (A tempting but unsupported answer would be: {q.get('adversarial_answer')})")
                qid = f"{sample['sample_id']}-c{cat}-{j}" + ("_abs" if cat == 5 else "")
                out.append({
                    "question_id": qid, "question_type": CATEGORY[cat], "question": q["question"],
                    "answer": str(gold), "rubric": None,
                    "question_date": (last + dt.timedelta(days=1)).strftime("%Y/%m/%d (%a) %H:%M"),
                    "haystack_session_ids": ids, "haystack_dates": dates, "haystack_sessions": sessions,
                    "answer_session_ids": sorted({f"{sample['sample_id']}-session_{e.split(':')[0][1:]}"
                                                  for e in q.get("evidence", []) if re.match(r"D\d+:", e)}),
                })
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    n_msgs = {q["haystack_session_ids"][0]: sum(len(s) for s in q["haystack_sessions"]) for q in out}
    print(f"{len(out)} questions from {len(n_msgs)} conversations, messages per conversation: {list(n_msgs.values())} -> {OUT}")


if __name__ == "__main__":
    main()
