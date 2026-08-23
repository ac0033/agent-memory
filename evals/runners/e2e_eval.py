"""端到端评估 runner（M2/M4b）：蒸馏写路径 + 检索注入 + 判定的全链路评估。

对每条用例：临时目录建库（绝不用真实 data_dir）→ 逐 session 走
蒸馏 → 评价门 → 对账 灌库 → 全部灌完后用 question 做混合检索 + render_recall_block
产出上下文块 → 判定。layer3（跨会话隐藏关联）额外在上下文块前拼
build_system_context 的常驻画像块——layer3 评的就是"常驻概览 + 按需细节"双层配合。

两种模式：
- 真实模式：配置了 LLM（AGENT_MEMORY_LLM_API_KEY），蒸馏与对账决策走真实模型；
  判定默认规则判定，加 --llm-judge 后由评委模型按 rubric 打分
  （essential 全覆盖 + pitfalls 触发即 FAIL 一票否决）。
- 规则降级模式：无 LLM key 时自动降级。灌库用用例自带的 memories（layer2/3 按
  session 分布；layer1 用 memories_expected 兜底），对账决策用脚本化 oracle
  （supersedes 字段 → UPDATE，否则 ADD）；判定为 reference_answer 关键词命中
  + expected memory id 在库 + 召回命中。输出里会显著标注"规则降级模式"。

M4b 新增：
- --baseline：同一批用例再在空库（不灌任何记忆）下跑一遍，与有记忆组逐题配对
  比较（McNemar 精确检验 p 值 + 配对 bootstrap 增益 95% CI；样本 < 20 时显式
  标注"不足以下强结论"且不并入发布门槛）；
- 进化指标埋点：激活率（写入记忆被召回比例，检索带 track_retrieval）、
  遵循率（评委确认判定依据来自召回记忆的用例比例）、留出增益（两组分差）。

运行：uv run python evals/runners/e2e_eval.py --layers 1,2
提速：用例级并发 --jobs N（默认 4）；LLM 响应磁盘缓存默认开（data/logs/llm_cache/，
换模型自动不命中），--no-cache 关闭；429 限流自动指数退避重试。
退出码：所有层准确率达到门槛（layer1 1.0 / layer2 0.8 / layer3 0.8）为 0，否则 1。
"""

import argparse
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

# 允许直接以脚本方式运行（uv run python evals/runners/e2e_eval.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
# metrics.py 与本脚本同目录（evals/runners 不是包），直接脚本方式运行时需显式入 path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics  # noqa: E402

from agent_memory.config import Settings, get_settings  # noqa: E402
from agent_memory.llm import LLMError, OpenAILLMClient  # noqa: E402
from agent_memory.long_term.ingest.distill import distill_memories  # noqa: E402
from agent_memory.long_term.ingest.gate import gate_candidates  # noqa: E402
from agent_memory.long_term.ingest.reconcile import reconcile  # noqa: E402
from agent_memory.long_term.retrieve.embedder import get_embedder  # noqa: E402
from agent_memory.long_term.retrieve.hybrid import HybridSearcher  # noqa: E402
from agent_memory.long_term.retrieve.inject import render_recall_block  # noqa: E402
from agent_memory.long_term.retrieve.resident import build_system_context  # noqa: E402
from agent_memory.long_term.store.index_db import IndexDB  # noqa: E402
from agent_memory.long_term.store.markdown_store import MarkdownStore  # noqa: E402
from agent_memory.models import EvidenceRef, MemoryEntry  # noqa: E402

DATASETS_DIR = Path(__file__).resolve().parent.parent / "datasets"
REPO_ROOT = Path(__file__).resolve().parents[2]
# LLM 响应磁盘缓存目录（data/ 已 gitignored）：重跑只改部分环节时秒级复用其余响应
LLM_CACHE_DIR = REPO_ROOT / "data" / "logs" / "llm_cache"
TOP_K = 5
# 各层准确率门槛
LAYER_THRESHOLDS = {1: 1.0, 2: 0.8, 3: 0.8}

# 429 / 限流重试：间隔递增，最多 4 次（与 prefix_regression 同一套逻辑）
_MAX_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = [10, 20, 40]

# 规则判定的关键词命中 floor：拉丁 token 覆盖率与中文 bigram 命中率取 OR
# （释义会换词，单路阈值会被误伤；空/无关块两路都扑空仍会被拦下）
RULE_TOKEN_MIN_COVERAGE = 0.5
RULE_BIGRAM_MIN_COVERAGE = 0.5


# ---------------------------------------------------------------- 数据结构


@dataclass
class CaseResult:
    case_id: str
    layer: int
    passed: bool
    # write_ok / recall_hit 基于 expected memory id，只有规则降级模式（mock 灌库，
    # id 由用例指定）才有意义；真实模式蒸馏自由命名，id 恒不命中，故置 None 表示 N/A，
    # PASS/FAIL 以评委 / 关键词判定为准
    write_ok: bool | None = None  # expected memory id 全部在库
    recall_hit: bool | None = None  # expected id 出现在 top5
    keyword_ok: bool = False  # reference_answer 关键词命中（规则模式）
    judge_detail: str = ""
    feed_seconds: float = 0.0  # 灌库阶段耗时（蒸馏 + 评价门 + 对账 + 嵌入）
    judge_seconds: float = 0.0  # 判定阶段耗时（评委 / 关键词）
    # ---- M4b 进化指标埋点（baseline 空库组为 None 表示 N/A）----
    # 激活率埋点：本用例库中被召回过（retrieval_count > 0）的记忆占比
    activated_ratio: float | None = None
    # 遵循率埋点：评委判定"判定所依据的事实实际来自召回记忆"（仅 --llm-judge 有值）
    used_memories: bool | None = None


@dataclass
class EvalReport:
    mode: str  # "real" | "rule-degraded"
    llm_judge: bool
    results: list[CaseResult] = field(default_factory=list)

    def accuracy_by_layer(self) -> dict[int, float]:
        out = {}
        layers = sorted({r.layer for r in self.results})
        for layer in layers:
            rs = [r for r in self.results if r.layer == layer]
            out[layer] = sum(r.passed for r in rs) / len(rs) if rs else 0.0
        return out


# ---------------------------------------------------------------- 限流重试


def _is_rate_limit(exc: Exception) -> bool:
    """openai.RateLimitError 或 message 里带 429 的 API 错误。"""
    if type(exc).__name__ == "RateLimitError":
        return True
    return "429" in str(exc) or "rate" in str(exc).lower() and "limit" in str(exc).lower()


def _run_case_with_retry(
    case_file: Path, embedder, settings, *, llm=None, judge_llm=None, feed: bool = True
):
    """整条用例级别的 429/限流指数退避重试（用例用独立临时目录，重跑幂等）。"""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return run_case(case_file, embedder, settings, llm=llm, judge_llm=judge_llm, feed=feed)
        except Exception as e:
            if attempt < _MAX_ATTEMPTS - 1 and _is_rate_limit(e):
                wait = _RETRY_BACKOFF_SECONDS[attempt]
                print(f"  [retry] {case_file.name} 触发限流，{wait}s 后重试（第 {attempt + 2} 次）")
                time.sleep(wait)
                continue
            raise


# ---------------------------------------------------------------- 灌库


def _yaml_mem_to_entry(mem: dict, case_id: str, session_id: str) -> MemoryEntry:
    """把用例 YAML 里的记忆条目转成 MemoryEntry（mock 灌库用）。"""
    today = date.today()
    return MemoryEntry(
        id=mem["id"],
        content=mem["content"],
        memory_type=mem["memory_type"],
        scope="global",
        confidence="high",
        source=f"eval:{case_id}",
        evidence=[EvidenceRef(session_id=session_id, source=f"eval:{case_id}")],
        created_at=today,
        last_verified=today,
    )


class ScriptedLLM:
    """规则降级模式的对账决策 oracle：supersedes → UPDATE，否则 ADD。

    蒸馏（complete）在降级模式下不被调用——候选直接来自用例 YAML。
    """

    def __init__(self, oracle: dict[str, dict]):
        self.oracle = oracle

    def complete(self, system: str, user: str) -> str:
        raise LLMError("ScriptedLLM 不支持 complete（规则降级模式不跑蒸馏）")

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        match = re.search(r"新记忆候选：\n- id: (\S+)", user)
        candidate_id = match.group(1) if match else ""
        return self.oracle.get(
            candidate_id, {"action": "ADD", "target_id": None, "reason": "oracle 默认"}
        )


def feed_case_mock(case: dict, store, index, embedder, settings) -> None:
    """规则降级模式灌库：用例自带 memories 逐 session 过 评价门 → 对账。"""
    sessions_with_mems = [s for s in case["sessions"] if s.get("memories")]
    if sessions_with_mems:
        batches = [(s["session_id"], s["memories"]) for s in case["sessions"]]
    else:
        # layer1 没有 per-session memories：memories_expected 兜底，挂到最后一个 session
        batches = [
            (s["session_id"], []) for s in case["sessions"][:-1]
        ] + [(case["sessions"][-1]["session_id"], case["memories_expected"])]

    oracle = {}
    for _sid, mems in batches:
        for m in mems:
            if m.get("supersedes"):
                oracle[m["id"]] = {
                    "action": "UPDATE",
                    "target_id": m["supersedes"],
                    "reason": "mock oracle：supersedes 指定的 UPDATE",
                }
            else:
                oracle[m["id"]] = {"action": "ADD", "target_id": None, "reason": "mock oracle"}
    llm = ScriptedLLM(oracle)

    for session_id, mems in batches:
        candidates = [_yaml_mem_to_entry(m, case["id"], session_id) for m in mems]
        if not candidates:
            continue
        gate_result = gate_candidates(candidates, Path(store.data_dir))
        for _entry, reason in gate_result.rejected:
            raise RuntimeError(f"{case['id']} 用例候选被评价门拒绝（用例数据问题）: {reason}")
        reconcile(
            gate_result.passed, store, index, llm, embedder=embedder, settings=settings
        )


def feed_case_real(case: dict, store, index, embedder, settings, llm) -> None:
    """真实模式灌库：逐 session 走 蒸馏 → 评价门 → 对账 全管线。"""
    for session in case["sessions"]:
        result = distill_memories(
            session["turns"], "global", f"eval:{case['id']}", session["session_id"], llm
        )
        gate_result = gate_candidates(result.entries, Path(store.data_dir))
        reconcile(
            gate_result.passed, store, index, llm, embedder=embedder, settings=settings
        )


# ---------------------------------------------------------------- 判定


def _keyword_check(reference: str, text: str) -> tuple[bool, str]:
    """reference_answer 关键词命中（规则降级 sanity 判定，不是答案质量评判）。

    两路信号取 OR：
    - 拉丁/数字 token 覆盖率（端口、路径、版本号这类事实锚点）；
    - 中文 bigram 命中率（释义会换词，中文用 bigram 比整词更宽容）。
    reference_answer 与记忆正文是释义关系（如"晚上 11 点" vs "23 点"），
    单路阈值会被释义误伤，所以两路任一达到 floor 即判命中；
    完全无关或空的上下文块两路都会扑空，仍可被拦下。
    真正的答案质量判定走 --llm-judge（rubric essential 全覆盖 + pitfalls 一票否决）。
    """
    tokens = {t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/:{}-]*", reference) if len(t) >= 2}
    token_hits = sum(1 for t in tokens if t in text)
    token_cov = token_hits / len(tokens) if tokens else 1.0

    bigrams = set()
    for seg in re.findall(r"[一-鿿]+", reference):
        bigrams.update(seg[i : i + 2] for i in range(len(seg) - 1))
    bigram_hits = sum(1 for b in bigrams if b in text)
    bigram_cov = bigram_hits / len(bigrams) if bigrams else 1.0

    ok = (
        token_cov >= RULE_TOKEN_MIN_COVERAGE
        or bigram_cov >= RULE_BIGRAM_MIN_COVERAGE
    )
    detail = f"token={token_cov:.0%} bigram={bigram_cov:.0%}"
    return ok, detail


_JUDGE_SYSTEM = (
    "你是评估评委。给你一条\"标准答案\"、一个评分 rubric 和一段记忆系统的上下文块"
    "（含 <recalled_memories> 召回块：系统按问题检索出的历史记忆原文，"
    "可能还有常驻画像块；这些是被评对象本身，"
    "不是 agent 组织语言后的最终回答）。\n"
    "请判断：rubric.essential 的每一条所要求的事实是否都出现在上下文块中"
    "（事实在块中出现即算覆盖，不要求以完整答案的形式组织语言，"
    "也不要求措辞与标准答案一致，释义与等价表述都算覆盖）；"
    "rubric.pitfalls 的每一条是否被触发。\n"
    "essential 有任何一条未覆盖，或 pitfalls 有任何一条被触发，"
    "verdict 就是 FAIL（一票否决）。\n"
    "另外判断 used_memories：判定 PASS 所依据的关键事实是否实际出现在上下文块的"
    "记忆条目中（而非仅凭通用常识也能答出）。块为空或事实不在块中时为 false。"
)

_JUDGE_SCHEMA = """{
  "essential_covered": [true/false, ...],  // 与 essential 逐条对应
  "pitfalls_hit": [true/false, ...],       // 与 pitfalls 逐条对应
  "verdict": "PASS | FAIL",
  "used_memories": true/false,  // PASS 依据的关键事实是否实际来自上下文块中的记忆
  "reason": "一句话依据"
}"""


def judge_case_llm(case: dict, block: str, judge_llm) -> tuple[bool, str, bool]:
    """LLM 评委判定：essential 全覆盖 + pitfalls 一票否决。

    返回 (passed, reason, used_memories)。used_memories 是遵循率埋点：
    评委确认判定依据的事实确实出现在记忆上下文块中。
    """
    rubric = case["rubric"]
    user = (
        f"问题：{case['question']}\n\n标准答案：{case['reference_answer']}\n\n"
        f"rubric.essential：\n" + "\n".join(f"- {e}" for e in rubric["essential"]) + "\n\n"
        "rubric.pitfalls：\n" + "\n".join(f"- {p}" for p in rubric["pitfalls"]) + "\n\n"
        f"agent 的上下文块：\n{block or '（空）'}"
    )
    parsed = judge_llm.complete_json(_JUDGE_SYSTEM, user, _JUDGE_SCHEMA)
    essential = parsed.get("essential_covered", [])
    pitfalls = parsed.get("pitfalls_hit", [])
    passed = (
        all(essential)
        and len(essential) == len(rubric["essential"])
        and not any(pitfalls)
    )
    return passed, str(parsed.get("reason", "")), bool(parsed.get("used_memories", False))


def run_case(
    case_file: Path, embedder, settings: Settings, *, llm=None, judge_llm=None, feed: bool = True
) -> CaseResult:
    """单条用例全流程：临时目录灌库 → 检索 → 渲染 → 判定。

    feed=False 是 baseline 模式（配对比较的空库组）：不灌任何记忆，
    用例、问题、判定口径与有记忆组完全一致，唯一自变量是库里有没有记忆。
    """
    case = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    expected_ids = [m["id"] for m in case["memories_expected"]]
    with tempfile.TemporaryDirectory() as tmp:
        case_settings = settings.model_copy(update={"data_dir": Path(tmp)})
        store = MarkdownStore(Path(tmp))
        index = IndexDB(Path(tmp) / "index.db")
        try:
            t0 = time.perf_counter()
            if feed:
                if llm is not None:
                    feed_case_real(case, store, index, embedder, case_settings, llm)
                else:
                    feed_case_mock(case, store, index, embedder, case_settings)

            searcher = HybridSearcher(store, index, embedder, case_settings)
            results = searcher.search(
                case["question"], scopes=["global"], k=TOP_K, track_retrieval=True
            )
            block = render_recall_block(results, case_settings.recall_budget_chars)
            if case["layer"] == 3:
                # layer3 是"双层配合"题：常驻 profile 概览（resident）+ 按需检索细节。
                # 评委看到的上下文块 = 常驻画像块 + 召回块，与真实注入链路一致
                resident = build_system_context("global", store=store, settings=case_settings)
                if resident:
                    block = f"{resident}\n\n{block}" if block else resident
            t1 = time.perf_counter()

            stored_entries = store.list()
            stored_ids = {e.id for e in stored_entries}
            top_ids = [r.entry.id for r in results]
            if feed and stored_entries:
                # 激活率埋点：被召回过的记忆占全部写入记忆的比例
                activated_ratio = (
                    sum(1 for e in stored_entries if e.retrieval_count > 0) / len(stored_entries)
                )
            else:
                activated_ratio = None  # baseline 空库：激活率 N/A
            if llm is None and feed:
                write_ok = set(expected_ids) <= stored_ids
                recall_hit = bool(set(top_ids) & set(expected_ids))
            else:
                # 真实模式：蒸馏自由命名，expected id 恒不命中，write_ok/recall_hit
                # 无区分度，记 N/A（None）；baseline 空库同样无意义。
                # 语义等价性由评委 / 关键词判定覆盖
                write_ok = None
                recall_hit = None

            if judge_llm is not None:
                passed, detail, used_memories = judge_case_llm(case, block, judge_llm)
                keyword_ok = True  # LLM 判定模式下不做关键词检查
            else:
                used_memories = None  # 遵循率埋点只在评委模式下有值
                keyword_ok, kw_detail = _keyword_check(case["reference_answer"], block)
                if llm is not None or not feed:
                    # 真实模式无评委 / baseline 空库：write_ok/recall_hit 为 N/A，
                    # 退化为关键词判定
                    passed = keyword_ok
                else:
                    passed = write_ok and recall_hit and keyword_ok
                detail = kw_detail
            t2 = time.perf_counter()
        finally:
            index.close()
    return CaseResult(
        case_id=case["id"],
        layer=case["layer"],
        passed=passed,
        write_ok=write_ok,
        recall_hit=recall_hit,
        keyword_ok=keyword_ok,
        judge_detail=detail,
        feed_seconds=t1 - t0,
        judge_seconds=t2 - t1,
        activated_ratio=activated_ratio,
        used_memories=used_memories,
    )


# ---------------------------------------------------------------- 入口


def load_case_files(layers: list[int]) -> list[Path]:
    files = []
    for layer in layers:
        files.extend(sorted((DATASETS_DIR / f"layer{layer}").glob("*.yaml")))
    return files


def filter_case_files(case_files: list[Path], case_id: str) -> list[Path]:
    """按用例 id（YAML 里的 id 字段，如 layer1-05）过滤，供单用例诊断。"""
    out = []
    for f in case_files:
        if yaml.safe_load(f.read_text(encoding="utf-8")).get("id") == case_id:
            out.append(f)
    return out


def run_once(
    case_files, embedder, settings, *, llm=None, judge_llm=None, jobs: int = 1, feed: bool = True
) -> EvalReport:
    """跑一轮用例。jobs>1 时用有界线程池并发（每条用例独立临时目录，天然隔离；
    共享的 embedder 已在内部加锁，llm 客户端与磁盘缓存均并发安全）。
    结果顺序与 case_files 一致，与 jobs 无关。
    feed=False 即 baseline 空库组（配对比较用）。"""
    report = EvalReport(
        mode=("real" if llm is not None else "rule-degraded") + ("" if feed else "+baseline"),
        llm_judge=judge_llm is not None,
    )
    if jobs <= 1:
        for f in case_files:
            report.results.append(
                _run_case_with_retry(f, embedder, settings, llm=llm, judge_llm=judge_llm, feed=feed)
            )
        return report
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(
                _run_case_with_retry, f, embedder, settings,
                llm=llm, judge_llm=judge_llm, feed=feed,
            )
            for f in case_files
        ]
        for fut in futures:
            report.results.append(fut.result())
    return report


def main() -> int:
    # Windows 控制台默认 GBK，中文报告会乱码；强制 UTF-8 输出
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="agent-memory 端到端评估（M2）")
    parser.add_argument("--layers", default="1,2", help="逗号分隔的层号，如 1,2")
    parser.add_argument("--seeds", type=int, default=1, help="跑多少个种子，报均值与区间")
    parser.add_argument(
        "--seed", type=int, default=42, help="起始种子（mock 模式下无实际作用，接口预留）"
    )
    parser.add_argument("--llm-judge", action="store_true", help="用真实 LLM 评委按 rubric 判定")
    parser.add_argument("--case", default=None, help="只跑指定用例 id（如 layer1-05），诊断用")
    parser.add_argument(
        "--jobs", type=int, default=4, help="用例级并发度（线程池，默认 4；1 为串行）"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=f"禁用 LLM 响应磁盘缓存（默认开，目录 {LLM_CACHE_DIR.relative_to(REPO_ROOT)}）",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="配对比较模式：同一批用例再在空库（不灌任何记忆）下跑一遍，"
        "输出逐题胜负、McNemar p 值、配对 bootstrap 增益区间与三个进化指标",
    )
    args = parser.parse_args()

    layers = [int(x) for x in args.layers.split(",")]
    case_files = load_case_files(layers)
    if args.case:
        case_files = filter_case_files(case_files, args.case)
    if not case_files:
        print(f"未找到评估用例（layers={layers}）", file=sys.stderr)
        return 2

    settings = get_settings()
    cache_dir = None if args.no_cache else LLM_CACHE_DIR

    llm = None
    try:
        llm = OpenAILLMClient.from_settings(settings, cache_dir=cache_dir)
    except LLMError:
        pass

    judge_llm = None
    if args.llm_judge:
        judge_settings = settings.model_copy(
            update={
                "llm_base_url": settings.judge_llm_base_url or settings.llm_base_url,
                "llm_api_key": settings.judge_llm_api_key or settings.llm_api_key,
                "llm_model": settings.judge_llm_model or settings.llm_model,
            }
        )
        try:
            judge_llm = OpenAILLMClient.from_settings(judge_settings, cache_dir=cache_dir)
        except LLMError as e:
            print(f"--llm-judge 需要可用的评委 LLM：{e}", file=sys.stderr)
            return 2

    if llm is None:
        print("=" * 64)
        print("!! 规则降级模式：未配置 LLM（AGENT_MEMORY_LLM_API_KEY）")
        print("!! 灌库走用例自带 memories + 脚本化对账 oracle，判定为关键词规则判定")
        print("!! 结果不代表真实蒸馏质量，真实端点验收需另行补做")
        print("=" * 64)

    embedder = get_embedder(settings)

    per_seed_accuracy: list[dict[int, float]] = []
    wall_start = time.perf_counter()
    for i in range(args.seeds):
        seed = args.seed + i  # noqa: F841  mock 模式下种子无实际作用，接口预留
        report = run_once(
            case_files, embedder, settings, llm=llm, judge_llm=judge_llm, jobs=args.jobs
        )
        per_seed_accuracy.append(report.accuracy_by_layer())
    wall_seconds = time.perf_counter() - wall_start

    # 明细只打最后一个种子的（mock 模式各种子结果相同）
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        flags = []
        if r.write_ok is False:
            flags.append("写库缺失")
        if r.recall_hit is False:
            flags.append("召回未命中")
        if not r.keyword_ok:
            flags.append("关键词未命中")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        print(
            f"[{mark}] {r.case_id}{suffix} {r.judge_detail} "
            f"[灌库 {r.feed_seconds:.1f}s / 判定 {r.judge_seconds:.1f}s]"
        )

    print()
    all_pass = True
    for layer in layers:
        accs = [a.get(layer, 0.0) for a in per_seed_accuracy]
        mean = sum(accs) / len(accs)
        interval = f"[{min(accs):.0%}, {max(accs):.0%}]" if len(accs) > 1 else ""
        threshold = LAYER_THRESHOLDS.get(layer, 0.8)
        status = "达标" if mean >= threshold else "未达标"
        if mean < threshold:
            all_pass = False
        print(
            f"layer{layer}: 准确率均值 {mean:.2%} {interval}（门槛 {threshold:.0%}，{status}）"
        )

    # 耗时构成：case 耗时和 ≈ 串行基线，与墙钟时间的差即并发收益
    feed_sum = sum(r.feed_seconds for r in report.results)
    judge_sum = sum(r.judge_seconds for r in report.results)
    print()
    case_sum = feed_sum + judge_sum
    print(
        f"耗时：墙钟 {wall_seconds:.1f}s（jobs={args.jobs}，缓存{'关' if args.no_cache else '开'}）"
        f"；用例耗时合计 {case_sum:.1f}s（灌库 {feed_sum:.1f}s + 判定 {judge_sum:.1f}s）"
    )

    if args.baseline:
        all_pass = _run_baseline_and_print_paired(
            case_files, report, embedder, settings,
            llm=llm, judge_llm=judge_llm, jobs=args.jobs,
        ) and all_pass
    return 0 if all_pass else 1


def _run_baseline_and_print_paired(
    case_files, report_mem: EvalReport, embedder, settings, *, llm, judge_llm, jobs: int
) -> bool:
    """配对比较：同一批用例空库再跑一遍，输出逐题胜负 + McNemar + bootstrap + 三指标。

    返回配对比较是否支持"有记忆显著优于空库"（p < 0.05 且增益区间下界 > 0）。
    样本不足时不参与 all_pass 判定，只作方向性参考。
    """
    print()
    print("=" * 64)
    print("配对比较：同一批用例在空库（baseline，不灌任何记忆）下重跑")
    print("=" * 64)
    t0 = time.perf_counter()
    report_base = run_once(
        case_files, embedder, settings, llm=llm, judge_llm=judge_llm, jobs=jobs, feed=False
    )
    base_seconds = time.perf_counter() - t0

    by_id_mem = {r.case_id: r for r in report_mem.results}
    by_id_base = {r.case_id: r for r in report_base.results}
    for case_id in by_id_mem:
        m, b = by_id_mem[case_id], by_id_base[case_id]
        if m.passed and not b.passed:
            outcome = "记忆组胜"
        elif b.passed and not m.passed:
            outcome = "baseline 胜"
        else:
            outcome = "平"
        print(
            f"  {case_id}: 有记忆={'PASS' if m.passed else 'FAIL'} / "
            f"baseline={'PASS' if b.passed else 'FAIL'} → {outcome}"
        )

    stats = metrics.paired_stats(
        [r.passed for r in report_mem.results],
        [by_id_base[r.case_id].passed for r in report_mem.results],
    )
    acc_mem = sum(r.passed for r in report_mem.results) / len(report_mem.results)
    acc_base = sum(r.passed for r in report_base.results) / len(report_base.results)
    print()
    print(f"有记忆组准确率 {acc_mem:.2%}，baseline 组准确率 {acc_base:.2%}")
    print(
        f"逐题胜负：双过 {stats.both_pass} / 双败 {stats.both_fail} / "
        f"记忆组独胜 {stats.memory_only} / baseline 独胜 {stats.baseline_only}"
    )
    print(f"McNemar 精确检验 p = {stats.mcnemar_p:.4f}")
    print(
        f"留出增益（有记忆 - baseline）= {stats.gain:+.2%}，"
        f"配对 bootstrap 95% CI [{stats.gain_ci_lo:+.2%}, {stats.gain_ci_hi:+.2%}]"
    )
    if stats.small_sample:
        print(
            f"!! 样本仅 {stats.n} 条（< {metrics.SMALL_SAMPLE_MIN}），"
            "p 值与置信区间只作方向性参考，不足以下强结论（不影响退出码）"
        )

    # 三个进化指标的埋点统计
    print()
    activation = metrics.mean_or_none([r.activated_ratio for r in report_mem.results])
    adherence_vals = [r.used_memories for r in report_mem.results if r.used_memories is not None]
    adherence = (
        sum(adherence_vals) / len(adherence_vals) if adherence_vals else None
    )
    print("进化指标（埋点统计）：")
    if activation is None:
        print("  激活率：N/A（无灌库结果）")
    else:
        print(f"  激活率：{activation:.2%}（写入的记忆被召回的比例，逐用例均值）")
    if adherence is None:
        print("  遵循率：N/A（未启用 --llm-judge）")
    else:
        print(
            f"  遵循率：{adherence:.2%}（评委确认判定依据实际来自召回记忆的用例比例，"
            f"n={len(adherence_vals)}）"
        )
    print(f"  留出增益：{stats.gain:+.2%}（见上方 CI）")
    print(f"baseline 组耗时：墙钟 {base_seconds:.1f}s")
    if stats.small_sample:
        return True  # 样本不足时不把配对显著性并入发布门槛
    return stats.mcnemar_p < 0.05 and stats.gain_ci_lo > 0


if __name__ == "__main__":
    sys.exit(main())
