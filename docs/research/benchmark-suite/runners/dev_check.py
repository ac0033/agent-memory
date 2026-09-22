"""自建验证集的精简 runner：集成测试的定位——快、能定位问题、可以反复跑。

两层，逐层更贵：
- 第 0 层（不调答题器、不调评委，建库走缓存后秒级）：每道题的载荷里证据标记是否到场、
  载荷体量相对"只检索原文"的倍数。证据没到场 = 检索/打包的问题；到场了还答错 = 呈现的问题。
- 第 1 层（--answer）：只跑被测系统，并发答题，规则判分；偏好/错误前提题才调评委。

完整的对照（朴素 RAG、全文上下文）用 aml_selftest.py 跑一次存档即可，不必每轮重跑。

    uv run python docs/research/benchmark-suite/runners/dev_check.py --run-id dev-b --answer --env-file <.env>
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import aml_selftest as A  # noqa: E402
from mc_common import DEFAULTS, read_env_file  # noqa: E402
from systems import AgentMemorySystem, NaiveRAGSystem, load_agent_memory, native_lock  # noqa: E402

# 验证集历史里的相对时间说法及其应当换算出的日期（见 build_dev_qa.py 的会话日期）
RELATIVE_TIME = [
    ("yesterday", "2024-02-11"), ("last saturday", "2024-03-02"), ("three weeks ago", "2024-02-05"),
    ("three weeks", "2024-04-10"), ("two months ago", "2023-11"),
]


def _query_rows(args, questions, am, rag, embedder, scope):
    rows = []
    for q in questions:
        with native_lock:
            items = am.svc.recall_items(q["question"], scope, k=args.top_k)
            rag_hits = rag.rag.search(q["question"], k=args.top_k)
        if args.strip_protocol and items and items[0]["id"] == "header":
            head = items[0]["content"]
            cut_from, cut_to = head.find("When a question asks"), head.find("Recorded conversations")
            if cut_from >= 0:
                items[0] = {**items[0], "content": head[:cut_from] + (head[cut_to:] if cut_to >= 0 else "")}
        if args.strip_profile:
            items = [x for x in items if x["id"] != "profile"]
        if args.strip_notes:
            items = [{**x, "content": x["content"].split("\nnotes:")[0]} for x in items if x["id"] != "profile"]
        if args.flat:
            with native_lock:
                raw_only = am.svc.raw_index.search(q["question"], embedder, k=args.top_k, scopes=[scope, "global"])
            items = [x for x in items if x["id"] in ("header", "profile")] + [
                {"id": f"raw:{h.session_id}:{h.line}", "content": f"[{h.date}] {h.role}: {h.content}"} for h in raw_only]
        payload = "\n".join(x["content"] for x in items)
        rag_payload = "\n".join(h.chunk.text for h in rag_hits)
        low, rag_low = payload.lower(), rag_payload.lower()
        marks = q.get("evidence") or []
        rows.append({
            "question_id": q["question_id"], "question_type": q["question_type"], "question": q["question"],
            "gold": q["answer"], "rubric": q.get("rubric"), "options": q.get("options"), "payload": payload,
            "rag_payload": "\n".join(f"[{h.chunk.date}] {h.chunk.role}: {h.chunk.text}" for h in rag_hits),
            "missing": [m for m in marks if m not in low],
            "rag_missing": [m for m in marks if m not in rag_low],
            "size_ratio": round(len(payload) / max(len(rag_payload), 1), 2),
            # 检索对等性：朴素 RAG 取到的原话里，有多少条也在我们的载荷里（不调 LLM）
            "rag_overlap": round(sum(1 for h in rag_hits if h.chunk.text[:160] in payload) / max(len(rag_hits), 1), 2)})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=A.REPO / "data" / "external" / "dev_qa_v1.json")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--buckets", default=None, help="只跑这些桶（逗号分隔）")
    ap.add_argument("--per-bucket", type=int, default=99)
    ap.add_argument("--answer", action="store_true", help="第 1 层：调答题器并判分")
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    ap.add_argument("--judge-model", default="glm-5.3-flash")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--strip-notes", action="store_true",
                    help="对照实验：去掉画像条目与各条里的 notes，只留原话（看注解是帮忙还是添乱）")
    ap.add_argument("--strip-profile", action="store_true", help="对照实验：去掉常驻画像块")
    ap.add_argument("--flat", action="store_true",
                    help="对照实验：不按片段打包，原文路命中逐条按名次给（与朴素 RAG 同形）")
    ap.add_argument("--with-rag", action="store_true", help="同场让朴素 RAG 的载荷也答一遍")
    ap.add_argument("--segment-gap", type=int, default=None,
                    help="对照实验：覆盖片段打包的行距阈值（极大值 = 退回整场会话打包）")
    ap.add_argument("--strip-protocol", action="store_true",
                    help="对照实验：把载荷首条里的行为协议句子去掉再答题（只改 runner 里的副本，不改被测系统）")
    args = ap.parse_args()

    questions = json.loads(args.data.read_text(encoding="utf-8"))
    if args.buckets:
        want = set(args.buckets.split(","))
        questions = [q for q in questions if q["question_type"] in want]
    seen: dict[str, int] = collections.Counter()
    picked = []
    for q in questions:
        seen[q["question_type"]] += 1
        if seen[q["question_type"]] <= args.per_bucket:
            picked.append(q)
    questions = picked

    env = read_env_file(args.env_file) if args.env_file else dict(os.environ)
    load_agent_memory(A.REPO)
    from agent_memory.config import get_settings
    from agent_memory.long_term.retrieve.embedder import get_embedder

    system_llm = A.CodeBuddyCLIClient("system", args.model)
    settings = get_settings().model_copy(update={
        "llm_api_key": env.get(DEFAULTS["system"]["key_env"]), "llm_model": f"codebuddy:{args.model}"})
    embedder = A.CachedEmbedder(get_embedder(settings), A.OUT_ROOT / "embedding_cache.pkl")
    log = lambda m: print(m, flush=True)  # noqa: E731


    if args.segment_gap is not None:
        from agent_memory.long_term.retrieve import recall as recall_mod

        recall_mod.SEGMENT_GAP = args.segment_gap
    out = A.OUT_ROOT / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    scope = f"repo:{A.SCOPE_TAG}"
    # 多份历史（外部 dev 样本）：按历史分组、逐组建库，行与库条目汇总后再报告
    groups: dict[str, list[dict]] = {}
    for q in questions:
        groups.setdefault(q["haystack_session_ids"][0], []).append(q)
    rows: list[dict] = []
    entries_all: list = []
    for gi, group in enumerate(groups.values(), 1):
        item = A.to_item(group[0])
        A.warm_embeddings(item, embedder, log)
        am = AgentMemorySystem(A.REPO, "am_v2", embedder, settings, system_llm)
        am.setup(item, mode="manual")
        add = A.am_add_all(am, item, log)
        rag = NaiveRAGSystem(embedder)
        rag.setup(item, mode="manual")
        log(f"built history {gi}/{len(groups)}: {add.get('entries_total')} entries, {add.get('add_seconds')}s")
        rows.extend(_query_rows(args, group, am, rag, embedder, scope))
        entries_all.extend(am.svc.store.list())
        am.close()
    # ---- 写入质量（A 层，组件级）：相对时间是否"只增不减"——原说法还在，换算出的日期附在后面
    entries = [e.content.lower() + " " + (e.detail or "").lower() for e in entries_all]
    print("\n== write check（相对时间：原说法 + 绝对日期）")
    for said, resolved in RELATIVE_TIME:
        both = sum(1 for t in entries if said in t and resolved in t)
        dated = sum(1 for t in entries if resolved in t)
        print(f"  {said!r:28s} -> {resolved}: 带日期的条目 {dated}，其中保留原说法 {both}")
    with_cues = sum(1 for e in entries_all if getattr(e, "cues", None))
    print(f"  条目总数 {len(entries)}，带线索键 {with_cues}")

    # ---- 第 0 层报告
    by = collections.defaultdict(list)
    for r in rows:
        by[r["question_type"]].append(r)
    print(f"\n== tier 0（证据到场 / 体量）  top_k={args.top_k}")
    print(f"{'bucket':7s} n  am到场 rag到场  体量比  与RAG原话重合")
    for b, g in by.items():
        print(f"{b:7s} {len(g):<2d} {sum(not r['missing'] for r in g):>5d} {sum(not r['rag_missing'] for r in g):>6d}"
              f"  {sum(r['size_ratio'] for r in g) / len(g):>5.2f}  {sum(r['rag_overlap'] for r in g) / len(g):>8.0%}")
    print(f"ALL     {len(rows):<2d} 与 RAG 原话重合 {sum(r['rag_overlap'] for r in rows) / len(rows):.0%}")
    for r in rows:
        if r["missing"]:
            print(f"  MISSING {r['question_id']}: {r['missing']}" + ("  (rag 也缺)" if r["rag_missing"] else "  (rag 有!)"))

    # ---- 第 1 层
    if args.answer:
        answerer = A.CodeBuddyAnswerer(A.CodeBuddyCLIClient("actor", args.model))
        judge = A.CodeBuddyCLIClient("judge", args.judge_model)

        def solve(r: dict) -> None:
            if r.get("options"):
                r["answer"] = answerer.complete(A.render_mcq_prompt(r["question"], r["options"], r["payload"]))
                m = re.search(r"\(([a-d])\)", r["answer"].lower())
                r["passed"] = bool(m) and f"({m.group(1)})" == r["gold"]
                return
            r["answer"] = answerer.complete(A.render_answer_prompt(r["question"], r["payload"]))
            if r["rubric"]:
                r["passed"] = A.rubric_pass(r["rubric"], r["answer"])
            else:
                r["passed"] = A.judge_label(judge, A.render_judge_prompt(r["question"], str(r["gold"]), r["answer"])) == "CORRECT"

        with ThreadPoolExecutor(max_workers=args.threads) as ex:
            list(ex.map(solve, rows))
        if args.with_rag:
            rag_rows = [{**r, "payload": r["rag_payload"]} for r in rows]
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                list(ex.map(solve, rag_rows))
            print(f"\n== 朴素 RAG 同场答题: {sum(r['passed'] for r in rag_rows)}/{len(rag_rows)}")
            for r, rr in zip(rows, rag_rows):
                r["rag_passed"] = rr["passed"]
        print("\n== tier 1（答题）")
        for b, g in by.items():
            print(f"{b:7s} {sum(r['passed'] for r in g)}/{len(g)}")
        print(f"TOTAL   {sum(r['passed'] for r in rows)}/{len(rows)}")
        for r in rows:
            if not r["passed"]:
                print(f"  FAIL {r['question_id']} gold={str(r['gold'])[:50]!r} ans={r['answer'][:140]!r}")
    (out / "dev_check.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
