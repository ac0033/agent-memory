"""AML 契约自测：按 Agent Memory Leaderboard 的 Add / Search 契约，在 LongMemEval-S（cleaned）上测 agent-memory。

目的（2026-09-16）：在决定是否参评之前，用 AML 的题、AML 的答题与评分提示词，量一次我们的记忆系统在
真实长历史（每题约 48 个会话、约 12 万 token）上的表现，并与朴素 RAG 对照。这正是 MemCompass 缺的 K4 长历史档位。

契约映射（public-benchmarks-survey.md §2.3）：
- Add：每个 haystack 会话调用一次 `MemoryService.add(conversation_json=...)`（归档 → 蒸馏 → 评价门 → 对账），
  user_id 对应一个隔离的数据目录 + scope（每题一个临时目录，题与题之间不共享任何状态）；
- Search：query 用题面原文，top_k=100；我们返回 记忆条目（混合检索，最多 40 条）+ 原文归档命中（补足到 100），
  每条 content 前缀会话日期。Search 不生成答案；
- 答题：AML `data/longmemeval-s/pipeline.py` 的 OPEN_ENDED_ANSWER_TEMPLATE 原文；评分：ACCURACY_PROMPT 原文，二值。

与正式评测的已知偏差（报告里必须写明）：
- 正式评测要求 Add/Search 内部与答题都用 gpt-4o-mini；本自测内部 LLM 与答题器是 deepseek-flash，评委是 Kimi K3（CLI）；
- 正式评测有平台私有题；本自测只有公开的 500 题中按题型分层抽样的一部分；
- 我们没有按 AML 的 20 条消息 / 2,000 词切分 Add 请求，整段会话一次写入。

用法（仓库根目录）：
  python docs/research/benchmark-suite/runners/aml_selftest.py --run-id aml-pilot --n-per-type 1 \
      --systems am,naive_rag --am-root . --am-label am_v2 --env-file <你的 .env>
  结果：data/logs/aml_selftest/<run-id>/{results.jsonl, meta.json, summary.md}；--resume 续跑。
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "pyproject.toml").exists())
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "docs" / "research" / "eval-drafts" / "runner-draft"))

from mc_common import (  # noqa: E402
    CACHE_DIR,
    CachedEmbedder,
    ChatClient,
    CodeBuddyCLIClient,
    KimiCLIClient,
    build_client,
    extract_usage,
    read_env_file,
)
from systems import AgentMemorySystem, NaiveRAGSystem, load_agent_memory, native_lock  # noqa: E402

OUT_ROOT = REPO / "data" / "logs" / "aml_selftest"
TOP_K = 100
MEM_K = 40  # 混合检索两路各取 20 候选，RRF 后最多 40 条
SCOPE_TAG = "lme"

# ---------------------------------------------------------------- AML 提示词（逐字复制自官方 pipeline.py）

OPEN_ENDED_ANSWER_TEMPLATE = """You are asked to answer a question based on your memories of a conversation.

<instructions>
1. Use only the provided memories. Prefer the memory that answers the question most directly.
2. Your memories are episodic raw observations. Reason about what they imply. Do not refuse just because the answer is not stated verbatim.
3. The question may contain typos. Match it to the most relevant memory even if the wording differs.
4. When multiple answers are possible, list all supported answers, not just the first.
5. For counts or time intervals, enumerate carefully before answering.
6. Preserve specific names, titles, places, and labels from the memories. Use "Rob" not "a colleague", "Sweden" not "home country".
7. Convert relative times like "yesterday", "last month", and "last year" into dates, months, or years when the memory timestamp makes it clear. Keep week-based expressions relative.
8. If memories conflict, prefer the most recent supported memory.
9. For list questions, include all required items and no extras.
10. Keep the final answer minimal. Do not add explanation, background, or extra dates unless needed for correctness.
</instructions>

<memories>
Memories for user {{speaker_1_name}}:

{{speaker_1_memories}}

Memories for user {{speaker_2_name}}:

{{speaker_2_memories}}
</memories>

Question: {{question}}
Answer with the shortest correct phrase or sentence. No preamble, no fluff:"""

ACCURACY_PROMPT = """Your task is to label an answer as ’CORRECT’ or ’WRONG’ given:
(1) a question,
(2) a gold (ground truth) answer,
(3) a generated answer.

Core principle — Inclusion + Non-contradiction
- Be GENEROUS: if the generated answer clearly includes the gold’s key content (or a clear paraphrase of the same content) and does not contradict it, mark CORRECT — even if extra details are added.
- Mark WRONG only when the generated answer does not include the gold’s content, changes it, or contradicts it.

TIME (strict granularity; relative form equivalence; no calendar math)
- Granularity must match exactly: HOUR↔HOUR, DAY↔DAY, MONTH↔MONTH, YEAR↔YEAR.
  Do not answer a gold at a different time unit — even if the numeric value overlaps. Do not answer a month-level gold with a specific day, nor a year with a specific month/day/hour, etc.
  (e.g., gold = "July 26, 2019" [DAY]; generated = "2019-07-26 08:09:17" [includes Second] → WRONG)
- Do NOT convert relative ↔ absolute. If the gold uses a relative time expression, the generated answer must also use a relative form (or a clear paraphrase of that same form), not a computed date/range.
- Treat harmless modifiers in relative forms (e.g., “the/last/previous/just prior”) as equivalent when both the anchor date and the time unit are the same.

- Lists of DISTINCT facts:
- If the gold answer lists multiple distinct facts (joined by "and", commas, or slashes), the generated answer must cover **all** of them.
- Extra non-contradictory items **generally count as WRONG**.
    - Example: gold = A, B, C ; gen = A, B, C → CORRECT
    - Example: gold = A, B, C ; gen = A, B, C, D → WRONG
- Exception: If a gold element is elaborated or split into finer details in the generated answer (e.g., C → C, C′), it is still considered CORRECT.

Preference/Benefit Questions (e.g., "what X likes/values most")
- If gold lists multiple reasons/aspects, the generated answer only needs to include **any one** of them without contradiction to be CORRECT.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label":

```json
{{
    "label": "CORRECT" or "WRONG"
}}
```"""


def render_answer_prompt(question: str, memories: str) -> str:
    values = {"speaker_1_name": "user", "speaker_1_memories": memories, "speaker_2_name": "assistant",
              "speaker_2_memories": "", "question": question}
    return re.sub(r"\{\{(speaker_1_name|speaker_1_memories|speaker_2_name|speaker_2_memories|question)\}\}",
                  lambda m: values[m.group(1)], OPEN_ENDED_ANSWER_TEMPLATE)


def render_judge_prompt(question: str, gold: str, generated: str) -> str:
    values = {"question": question, "gold_answer": gold, "generated_answer": generated}
    return re.sub(r"\{(question|gold_answer|generated_answer)\}", lambda m: values[m.group(1)], ACCURACY_PROMPT)


# ---------------------------------------------------------------- 数据

def iso_date(s: str) -> str:
    """'2023/05/20 (Sat) 02:21' -> '2023-05-20'（agent-memory 的 session_date 读前 10 位 ISO 日期）。"""
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else s[:10]


def load_questions(path: Path, n_per_type: int, seed: int, ids: list[str] | None) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if ids:
        want = set(ids)
        return [q for q in data if str(q["question_id"]) in want]
    rnd = random.Random(seed)
    by_type: dict[str, list[dict]] = collections.defaultdict(list)
    for q in data:
        by_type[q["question_type"]].append(q)
    picked = []
    for t in sorted(by_type):
        pool = sorted(by_type[t], key=lambda q: str(q["question_id"]))
        rnd.shuffle(pool)
        picked += pool[:n_per_type]
    return picked


def to_item(q: dict) -> dict:
    """转成 MemCompass runner 的 item 形状，复用 systems.py 的 setup。会话按日期排序（Add 顺序 = 时间顺序）。"""
    sessions = []
    for sid, d, msgs in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
        sessions.append({"session_id": str(sid), "date": iso_date(d), "raw_date": d, "scope": SCOPE_TAG,
                         "messages": [{"role": m["role"], "content": str(m["content"]).strip()} for m in msgs
                                      if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]})
    sessions.sort(key=lambda s: (s["date"], s["raw_date"]))
    return {"item_id": str(q["question_id"]), "history": {"sessions": sessions}}


# ---------------------------------------------------------------- 答题器（自由文本 + 缓存 + usage）

class TextClient:
    def __init__(self, base_url: str, api_key: str, model: str, cache: bool = True, timeout: float = 180):
        from openai import OpenAI

        self.model = model
        self.cache_dir = CACHE_DIR / "aml" if cache else None
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=2)
        self.last_usage: dict | None = None

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        # deepseek-flash 是推理模型：max_tokens 同时限制思考与可见回答，512 时思考就把额度用完、回答为空（先导轮实测）
        key = hashlib.sha256(json.dumps([self.model, prompt, max_tokens]).encode()).hexdigest()
        if self.cache_dir and (self.cache_dir / f"{key}.json").exists():
            rec = json.loads((self.cache_dir / f"{key}.json").read_text(encoding="utf-8"))
            self.last_usage = rec.get("usage")
            return rec["response"]
        backoff = [5, 15, 40]
        for attempt in range(len(backoff) + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=max_tokens)
                text = (resp.choices[0].message.content or "").strip()
                self.last_usage = extract_usage(resp)
                if self.cache_dir:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
                    tmp.write_text(json.dumps({"model": self.model, "response": text, "usage": self.last_usage},
                                              ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, self.cache_dir / f"{key}.json")
                return text
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if attempt < len(backoff) and any(t in msg for t in ("429", "rate", "timeout", "connection", "502", "503")):
                    time.sleep(backoff[attempt])
                    continue
                raise
        raise RuntimeError("unreachable")


class CodeBuddyAnswerer:
    """把 CodeBuddyCLIClient 适配成 TextClient 的 complete(prompt) 接口（答题器）。"""

    def __init__(self, client):
        self.client = client
        self.model = f"codebuddy:{client.model}"

    @property
    def last_usage(self):
        return self.client.last_usage

    def complete(self, prompt: str, max_tokens: int = 4096) -> str:
        return self.client.complete("", prompt)


def judge_label(judge, prompt: str) -> str:
    """评委按 AML 提示词打标签；Kimi CLI / OpenAI 兼容客户端都走 complete_json。"""
    out = judge.complete_json("", prompt, '{"label": "CORRECT" 或 "WRONG"}')
    label = str(out.get("label", "")).upper()
    if label not in ("CORRECT", "WRONG"):
        raise ValueError(f"judge label invalid: {out!r}")
    return label


# ---------------------------------------------------------------- 系统：Add / Search

def am_add_all(system: AgentMemorySystem, item: dict, log) -> dict:
    """逐会话 Add（真实写路径）。单个会话失败不终止整题，记入 errors。"""
    stats = {"sessions": len(item["history"]["sessions"]), "add_ok": 0, "add_errors": [], "entries_added": 0,
             "pending_review": 0, "add_seconds": 0.0}
    t0 = time.time()
    for i, s in enumerate(item["history"]["sessions"], 1):
        conv = json.dumps(s["messages"], ensure_ascii=False)
        try:
            r = system.svc.add(conversation_json=conv, scope=f"repo:{SCOPE_TAG}", source="lme",
                               session_id=s["session_id"], session_date=s["date"])
            stats["add_ok"] += 1
            stats["entries_added"] += len(r.get("added") or r.get("entries") or []) if isinstance(r, dict) else 0
            stats["pending_review"] += len(r.get("pending_review") or []) if isinstance(r, dict) else 0
        except Exception as e:  # noqa: BLE001
            stats["add_errors"].append({"session_id": s["session_id"], "error": f"{type(e).__name__}: {str(e)[:200]}"})
        if i % 10 == 0:
            log(f"    add {i}/{stats['sessions']} ({time.time() - t0:.0f}s)")
    stats["add_seconds"] = round(time.time() - t0, 1)
    with native_lock:
        try:
            stats["entries_total"] = len(system.svc.store.list())
        except Exception:  # noqa: BLE001
            stats["entries_total"] = None
    return stats


def am_search(system: AgentMemorySystem, query: str) -> list[dict]:
    """Search 契约：走服务的单一读路径 recall（memory-v1 M1），每束证据一条。

    没有字符预算（契约按条数取 top_k），所以原话不截断——截了就少于朴素 RAG 给的。
    每条带 session_id 便于算证据召回。"""
    from agent_memory.long_term.retrieve.recall import bundle_text

    scope = f"repo:{SCOPE_TAG}"
    with native_lock:
        _, bundles = system.svc.recall(query, scope, k=TOP_K)
    items: list[dict] = []
    for b in bundles:
        if b.entry is not None:
            sid = b.entry.evidence[0].session_id if b.entry.evidence else None
            items.append({"id": b.entry.id, "kind": "memory", "session_id": sid,
                          "content": bundle_text(b, excerpt_chars=None)})
        else:
            h = b.lines[0]
            items.append({"id": f"raw:{h.session_id}:{h.line}", "kind": "raw", "session_id": h.session_id,
                          "content": bundle_text(b, excerpt_chars=None)})
    return items[:TOP_K]


def rag_search(system: NaiveRAGSystem, query: str) -> list[dict]:
    with native_lock:
        hits = system.rag.search(query, k=TOP_K)
    return [{"id": f"{h.chunk.session_id}#{h.chunk.order}", "kind": "raw", "session_id": h.chunk.session_id,
             "content": f"[{h.chunk.date}] {h.chunk.role}: {h.chunk.text}"} for h in hits]


# ---------------------------------------------------------------- 主流程

def warm_embeddings(item: dict, embedder, log, batch: int = 32) -> None:
    """先把这题全部消息文本分批算进向量缓存并落盘。CPU 上一题约 500 条、20 多分钟，
    进程若中途被杀（本机内存紧张时常见），下次 --resume 只需补算剩余批次。"""
    texts = [m["content"] for s in item["history"]["sessions"] for m in s["messages"]]
    seen: set[str] = set()
    uniq = [t for t in texts if not (t in seen or seen.add(t))]
    t0 = time.time()
    for i in range(0, len(uniq), batch):
        embedder.embed_texts(uniq[i:i + batch])
        embedder.save()
        if (i // batch) % 4 == 3:
            log(f"    embed {min(i + batch, len(uniq))}/{len(uniq)} ({time.time() - t0:.0f}s)")


def run_one(q: dict, sys_name: str, ctx: dict, log) -> dict:
    item = to_item(q)
    qid = str(q["question_id"])
    warm_embeddings(item, ctx["embedder"], log)
    row = {"question_id": qid, "question_type": q["question_type"], "is_abstention": qid.endswith("_abs"),
           "system": sys_name, "question": q["question"], "gold": q["answer"], "question_date": q["question_date"],
           "n_sessions": len(item["history"]["sessions"]), "ts": dt.datetime.now().isoformat(timespec="seconds")}
    t_all = time.time()
    if sys_name == "am":
        system = AgentMemorySystem(ctx["am_root"], ctx["am_label"], ctx["embedder"], ctx["settings"], ctx["system_llm"])
        system.setup(item, mode="manual")
        try:
            row["add"] = am_add_all(system, item, log)
            t1 = time.time()
            items = am_search(system, q["question"])
            row["search_seconds"] = round(time.time() - t1, 2)
            row["cost_sys"] = system.meter.as_dict()
        finally:
            system.close()
    elif sys_name == "naive_rag":
        system = NaiveRAGSystem(ctx["embedder"])
        t0 = time.time()
        system.setup(item, mode="manual")
        row["add"] = {"sessions": len(item["history"]["sessions"]), "add_seconds": round(time.time() - t0, 1)}
        t1 = time.time()
        items = rag_search(system, q["question"])
        row["search_seconds"] = round(time.time() - t1, 2)
    else:
        raise ValueError(sys_name)
    answer_sids = set(map(str, q.get("answer_session_ids") or []))
    row["n_items"] = len(items)
    row["n_memory_items"] = sum(1 for x in items if x["kind"] == "memory")
    row["evidence_hit"] = bool(answer_sids) and any(str(x.get("session_id")) in answer_sids for x in items)
    row["evidence_rank"] = next((i + 1 for i, x in enumerate(items) if str(x.get("session_id")) in answer_sids), None)
    row["items_head"] = [x["content"][:160] for x in items[:5]]
    memories = "\n".join(x["content"] for x in items) if items else "(no memories)"
    prompt = render_answer_prompt(q["question"], memories)
    row["prompt_chars"] = len(prompt)
    answer = ctx["answerer"].complete(prompt)
    row["answer"] = answer
    row["cost_answer"] = ctx["answerer"].last_usage
    row["label"] = judge_label(ctx["judge"], render_judge_prompt(q["question"], str(q["answer"]), answer))
    row["judge"] = ctx["judge_name"]
    row["labels"] = {ctx["judge_name"]: row["label"]}
    row["passed"] = row["label"] == "CORRECT"
    row["total_seconds"] = round(time.time() - t_all, 1)
    return row


def summarize(rows: list[dict]) -> str:
    ok = [r for r in rows if not r.get("error")]
    systems = sorted({r["system"] for r in ok})
    types = sorted({r["question_type"] for r in ok})
    lines = ["# AML 契约自测（LongMemEval-S cleaned，公开题抽样）", "",
             "| 题型 | n | " + " | ".join(f"{s} 正确率" for s in systems) + " | " + " | ".join(f"{s} 证据召回@100" for s in systems) + " |",
             "|---|---|" + "---|" * (2 * len(systems))]

    def acc(g):
        return f"{sum(r['passed'] for r in g)}/{len(g)}" if g else "—"

    def rec(g):
        g = [r for r in g if r.get("evidence_rank") is not None or r.get("evidence_hit") is not None]
        return f"{sum(bool(r['evidence_hit']) for r in g)}/{len(g)}" if g else "—"

    for t in types + ["全部"]:
        cells = []
        recs = []
        n = 0
        for s in systems:
            g = [r for r in ok if r["system"] == s and (t == "全部" or r["question_type"] == t)]
            n = max(n, len(g))
            cells.append(acc(g))
            recs.append(rec(g))
        lines.append(f"| {t} | {n} | " + " | ".join(cells) + " | " + " | ".join(recs) + " |")
    lines.append("")
    for s in systems:
        g = [r for r in ok if r["system"] == s]
        if not g:
            continue
        add_s = sum((r.get("add") or {}).get("add_seconds") or 0 for r in g) / len(g)
        items = sum(r.get("n_items") or 0 for r in g) / len(g)
        mem_items = sum(r.get("n_memory_items") or 0 for r in g) / len(g)
        pc = sum(r.get("prompt_chars") or 0 for r in g) / len(g)
        line = f"- **{s}**：每题 Add 平均 {add_s:.0f}s，Search 返回 {items:.0f} 条（其中记忆条目 {mem_items:.0f}），答题提示 {pc:,.0f} 字符"
        cs = [r["cost_sys"] for r in g if r.get("cost_sys")]
        if cs:
            calls = sum(c["calls"] for c in cs) / len(cs)
            tok_in = sum(c.get("in_tokens", 0) for c in cs) / len(cs)
            tok_out = sum(c.get("out_tokens", 0) for c in cs) / len(cs)
            cov = sum(c.get("token_calls", 0) for c in cs) / max(1, sum(c["calls"] for c in cs))
            line += f"；系统内部 LLM 每题 {calls:.0f} 次调用、输入 {tok_in:,.0f} tok、输出 {tok_out:,.0f} tok（usage 覆盖 {cov:.0%}）"
            credit = sum(c.get("credit") or 0 for c in cs) / len(cs)
            ans_credit = sum((r.get("cost_answer") or {}).get("credit") or 0 for r in g) / len(g)
            if credit or ans_credit:
                line += f"；积分：系统 {credit:.2f} + 答题 {ans_credit:.2f} / 题"
        adds = [r["add"] for r in g if r.get("add") and "entries_total" in r["add"]]
        if adds:
            ent = sum(a.get("entries_total") or 0 for a in adds) / len(adds)
            errs = sum(len(a.get("add_errors") or []) for a in adds)
            line += f"；每题沉淀记忆 {ent:.0f} 条，Add 失败 {errs} 次"
        lines.append(line)
    errs = [r for r in rows if r.get("error")]
    if errs:
        lines += ["", "## 出错", ""] + [f"- {r['question_id']}/{r['system']}: {r['error'][:200]}" for r in errs[:20]]
    return "\n".join(lines) + "\n"


def deepseek_balance(env: dict) -> str | None:
    key = env.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        import httpx

        r = httpx.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        return ",".join(f"{b['currency']} {b['total_balance']}" for b in r.json().get("balance_infos", []))
    except Exception:  # noqa: BLE001
        return None


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "?"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=REPO / "data" / "external" / "longmemeval_s_cleaned.json")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--systems", default="am,naive_rag")
    ap.add_argument("--n-per-type", type=int, default=1, help="每个题型抽几题（6 个题型）")
    ap.add_argument("--ids", default=None, help="只跑这些 question_id（逗号分隔，覆盖抽样）")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--am-root", type=Path, default=REPO)
    ap.add_argument("--am-label", default="am_v2")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--answer-model", default=None, help="缺省 deepseek-flash（DEFAULTS.actor）")
    ap.add_argument("--judge-model", default=None, help="缺省 Kimi K3（CLI）")
    ap.add_argument("--judge-role", default="judge",
                    help="评委来源：judge（Kimi CLI）/ judge_qwen / judge_glm（token-plan）/ deepseek（与答题器同源，"
                         "只在其他评委额度耗尽时用，报告必须注明）")
    ap.add_argument("--answer-via", default="api", choices=["api", "codebuddy"],
                    help="答题器：api = DeepSeek 官方；codebuddy = WorkBuddy 内置 CLI（缺省模型 deepseek-v4-flash，--answer-model 可换）")
    ap.add_argument("--system-via", default="api", choices=["api", "codebuddy"],
                    help="被测系统内部 LLM（蒸馏 / 对账 / 浮现）：api = DeepSeek 官方（缺省）；codebuddy = WorkBuddy 内置 CLI")
    ap.add_argument("--system-model", default=None, help="--system-via codebuddy 时的模型（缺省 deepseek-v4-flash）")
    ap.add_argument("--rejudge", action="store_true",
                    help="不跑系统，只用 --judge-role 指定的评委重判 results.jsonl 里已有的回答，标签写入 labels[<评委>]")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    out = OUT_ROOT / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    done: set[tuple[str, str]] = set()
    if args.resume and results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if not r.get("error"):
                    done.add((r["question_id"], r["system"]))
    elif results.exists():
        raise SystemExit(f"{results} 已存在：换 --run-id 或加 --resume")

    env = read_env_file(args.env_file) if args.env_file else dict(os.environ)
    questions = load_questions(args.data, args.n_per_type, args.seed, args.ids.split(",") if args.ids else None)
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]

    load_agent_memory(args.am_root.resolve())
    from mc_common import DEFAULTS

    from agent_memory.config import get_settings
    from agent_memory.llm import OpenAILLMClient
    from agent_memory.long_term.retrieve.embedder import get_embedder

    settings = get_settings()
    sysd = DEFAULTS["system"]
    settings = settings.model_copy(update={
        "llm_api_key": env.get(sysd["key_env"]), "llm_base_url": sysd.get("base_url") or env.get(sysd.get("base_url_env", "")),
        "llm_model": sysd["model"]})
    if args.system_via == "codebuddy":
        system_llm = CodeBuddyCLIClient("system", args.system_model or "deepseek-v4-flash", cache=not args.no_cache)
        settings = settings.model_copy(update={"llm_model": f"codebuddy:{system_llm.model}"})
    else:
        system_llm = OpenAILLMClient.from_settings(settings, cache_dir=REPO / "data" / "logs" / "llm_cache" / f"sys-{args.am_label}-aml")
    embedder = CachedEmbedder(get_embedder(settings), OUT_ROOT / "embedding_cache.pkl")
    actor = DEFAULTS["actor"]
    if args.answer_via == "codebuddy":
        answerer = CodeBuddyAnswerer(CodeBuddyCLIClient("actor", args.answer_model or "deepseek-v4-flash", cache=not args.no_cache))
    else:
        answerer = TextClient(actor["base_url"], env[actor["key_env"]], args.answer_model or actor["model"], cache=not args.no_cache)
    if args.judge_role == "deepseek":
        judge = ChatClient("judge", actor["base_url"], env[actor["key_env"]], args.judge_model or actor["model"],
                           cache=not args.no_cache)
    else:
        judge = build_client(args.judge_role, env, args.judge_model, cache=not args.no_cache)
    judge_name = getattr(judge, "model", type(judge).__name__)
    if args.rejudge:
        rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
        n = 0
        for r in rows:
            if r.get("error") or "answer" not in r:
                continue
            labels = r.setdefault("labels", {})
            if judge_name in labels:
                continue
            labels[judge_name] = judge_label(judge, render_judge_prompt(r["question"], str(r["gold"]), r["answer"]))
            n += 1
        results.write_text("".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows), encoding="utf-8")
        print(f"rejudged {n} rows with {judge_name}")
        return
    ctx = {"am_root": args.am_root.resolve(), "am_label": args.am_label, "embedder": embedder, "settings": settings,
           "system_llm": system_llm, "answerer": answerer, "judge": judge, "judge_name": judge_name}

    bal0 = deepseek_balance(env)
    meta = {"run_id": args.run_id, "head": git_head(), "started": dt.datetime.now().isoformat(timespec="seconds"),
            "data": str(args.data), "n_questions": len(questions), "systems": systems, "seed": args.seed,
            "n_per_type": args.n_per_type, "answer_model": answerer.model,
            "judge": getattr(judge, "model", type(judge).__name__), "system_model": settings.llm_model,
            "judge_is_cli": isinstance(judge, KimiCLIClient), "balance_before": bal0,
            "embedding_max_seq_length": settings.embedding_max_seq_length,
            "question_ids": [str(q["question_id"]) for q in questions]}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    lock = threading.Lock()

    def log(msg: str) -> None:
        with lock:
            print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)

    def write(row: dict) -> None:
        with lock:
            with results.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    tasks = [(q, s) for q in questions for s in systems if (str(q["question_id"]), s) not in done]
    log(f"run {args.run_id}: {len(questions)} 题 × {systems}，待跑 {len(tasks)}（已完成 {len(done)}），余额 {bal0}")

    def work(q, s):
        qid = str(q["question_id"])
        log(f"start {qid} [{q['question_type']}] {s}")
        try:
            row = run_one(q, s, ctx, log)
            log(f"done  {qid} {s}: {row['label']} items={row['n_items']} evidence_rank={row['evidence_rank']} {row['total_seconds']}s")
        except Exception as e:  # noqa: BLE001
            row = {"question_id": qid, "question_type": q["question_type"], "system": s,
                   "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]}
            log(f"ERROR {qid} {s}: {row['error'][:200]}")
        write(row)
        try:
            embedder.save()
        except Exception:  # noqa: BLE001
            pass

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = [ex.submit(work, q, s) for q, s in tasks]
            for f in as_completed(futs):
                f.result()
    else:
        for q, s in tasks:
            work(q, s)

    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 同一 (题, 系统) 以最后一次为准（--resume 重跑出错行）
    latest: dict[tuple, dict] = {}
    for r in rows:
        latest[(r["question_id"], r["system"])] = r
    summary = summarize(list(latest.values()))
    bal1 = deepseek_balance(env)
    meta.update({"finished": dt.datetime.now().isoformat(timespec="seconds"), "balance_after": bal1})
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    summary += f"\n模型：系统内部 {settings.llm_model}，答题 {answerer.model}，评委 {meta['judge']}；代码 {meta['head']}；DeepSeek 余额 {bal0} → {bal1}\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
