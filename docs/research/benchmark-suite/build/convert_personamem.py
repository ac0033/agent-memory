"""把 PersonaMem（bowen-upenn/PersonaMem，MIT）的 32k 档转成 aml_selftest.py 能直接跑的形状。

PersonaMem 考个性化（K11）：给一段带偏好演变的多会话历史，用户说一句话，从四个候选回复里选
最贴合这位用户的那个。选择题，判分是确定性的（不需要评委）。

取题量最大的几份共享历史，只要"在整份历史末尾提问"的题——同一份历史的题共享一次写入。
历史按其中的 system 消息切成会话；system 消息（写着完整人设）不写入记忆：Add 契约只有
user / assistant 两种 role，而且把人设原文喂进去等于泄题。原数据没有时间戳，按会话顺序
每周一场补上日期。

数据不入库（data/ 已 gitignore）。外部集原则上只测一次。

    uv run python docs/research/benchmark-suite/build/convert_personamem.py --contexts 4
    → data/external/personamem/sample.json（已存：test_a / test_b 验收样本，dev_a / dev_b 诊断用）
"""

from __future__ import annotations

import argparse
import ast
import collections
import csv
import datetime as dt
import json
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
SRC = REPO / "data" / "external" / "personamem"
OUT = REPO / "data" / "external" / "personamem" / "sample.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contexts", type=int, default=4)
    ap.add_argument("--skip", type=int, default=0, help="跳过题量最大的前 N 份历史（留给验收用的不拿来诊断）")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    csv.field_size_limit(10**9)
    questions = list(csv.DictReader((SRC / "questions_32k.csv").open(encoding="utf-8")))
    contexts: dict[str, list[dict]] = {}
    for line in (SRC / "shared_contexts_32k.jsonl").open(encoding="utf-8"):
        contexts.update(json.loads(line))
    by_ctx: dict[str, list[dict]] = collections.defaultdict(list)
    for q in questions:
        by_ctx[q["shared_context_id"]].append(q)

    out = []
    for cid, group in sorted(by_ctx.items(), key=lambda kv: (-len(kv[1]), kv[0]))[args.skip : args.skip + args.contexts]:
        ctx = contexts[cid]
        end = len(ctx) - 1
        sessions: list[list[dict]] = []
        for m in ctx[:end]:
            if m["role"] == "system":
                sessions.append([])
                continue
            if not sessions:
                sessions.append([])
            text = m["content"].strip()
            for prefix in ("User:", "Assistant:"):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
            if text:
                sessions[-1].append({"role": m["role"], "content": text})
        sessions = [s for s in sessions if s]
        start = dt.datetime(2024, 1, 1, 10, 0)
        dates = [(start + dt.timedelta(weeks=i)).strftime("%Y/%m/%d (%a) %H:%M") for i in range(len(sessions))]
        ids = [f"pm-{cid[:8]}-s{i + 1}" for i in range(len(sessions))]
        for q in group:
            if int(q["end_index_in_shared_context"]) != end:
                continue
            out.append({
                "question_id": q["question_id"], "question_type": q["question_type"],
                "question": q["user_question_or_message"],
                "options": ast.literal_eval(q["all_options"]),
                "answer": q["correct_answer"].strip(), "rubric": None,
                "question_date": (start + dt.timedelta(weeks=len(sessions))).strftime("%Y/%m/%d (%a) %H:%M"),
                "haystack_session_ids": ids, "haystack_dates": dates, "haystack_sessions": sessions,
                "answer_session_ids": [],
            })
    args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    n = collections.Counter(q["haystack_session_ids"][0] for q in out)
    print(f"{len(out)} questions over {len(n)} histories {dict(n)} -> {args.out}")


if __name__ == "__main__":
    main()
