"""被测系统适配层：同一接口下的 5 类对照组（suite-design §5）。

| 系统 | 说明 |
|---|---|
| no_memory | 什么都没有 |
| full_context | 全部历史原文直接给答题器（本套件的历史都放得下） |
| oracle | 只给金标证据消息（上限参照，LongMemEval_oracle 的做法） |
| naive_rag | 原始消息切块，bge-m3 + BM25 → RRF（../../eval-drafts/runner-draft/naive_rag.py） |
| am | agent-memory；用 --am-root 指定代码根目录（基线 = HEAD 的 git worktree，优化版 = 工作区） |

公平性（suite-design §5）：答题器、评委、top_k、注入字符预算都相同。
agent-memory 的"接入规范"取自被测版本自己的 SKILL.md（首要原则 + 相关章节原文），
不为任何一方手写提示词；新接口（原文检索、主动浮现、确认队列、工作记忆刷新等）按
hasattr 自动探测，基线版本没有的能力就不提供对应工具。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

from mc_common import (
    REPO,
    CostMeter,
    MeteredLLM,
    native_lock,
    parse_time,
    render_session,
    session_date,
)

# 朴素 RAG 对照组：冻结副本（evals/memcompass/runners/）自带 naive_rag.py；编写源头从草稿目录导入
if not (Path(__file__).resolve().parent / "naive_rag.py").exists():
    sys.path.insert(0, str(REPO / "docs" / "research" / "eval-drafts" / "runner-draft"))

RAW_TOP_K = 5
MEM_TOP_K = 5
BUDGET = 2000


def scope_of(s: dict | None, default: str = "global") -> str:
    if not s or not s.get("scope") or s.get("scope") == "global":
        return default
    slug = re.sub(r"[^a-z0-9-]+", "-", str(s["scope"]).lower()).strip("-")
    return f"repo:{slug}"


def target_scope(item: dict) -> str:
    for p in item.get("probes") or []:
        lab = (p.get("gold") or {}).get("labels") or {}
        if lab.get("target_scope"):
            return scope_of({"scope": lab["target_scope"]})
    scopes = [scope_of(s) for s in item["history"]["sessions"] if s.get("scope")]
    return scopes[-1] if scopes else "global"


# ---------------------------------------------------------------- 基类


class System:
    name = "base"
    tools: set[str] = set()

    def guidance(self) -> str:
        return ""

    def setup(self, item: dict, mode: str) -> None:
        self.item = item

    def search(self, query: str, k: int = MEM_TOP_K, scope: str | None = None) -> tuple[str, str]:
        return "", ""

    def archive_search(self, query: str) -> str:
        return "（不可用）"

    def archive_read(self, session_id: str) -> str:
        return "（不可用）"

    def surface(self, turns: list[str], date: str, scope: str | None = None) -> str:
        return ""

    def session_start(self, scope: str, host: str | None, first_message: str | None = None) -> str:
        return ""

    def queue(self, message: str) -> str:
        return "错误：没有待确认队列"

    def state_read(self, scope: str, task: str | None = None) -> str:
        return "（没有工作记忆）"

    def wm_snapshot(self, scope: str) -> str:
        return ""

    def observe(self, messages: list[dict], event: str, scope: str, host_llm=None, turn_index: int = 0) -> None:
        pass

    def close(self) -> None:
        pass


class NoMemory(System):
    name = "no_memory"


class FullContext(System):
    name = "full_context"

    def _all(self) -> str:
        return "<history>\n" + "\n".join(render_session(s) for s in self.item["history"]["sessions"]) + "\n</history>"

    def search(self, query, k=MEM_TOP_K, scope=None):
        t = self._all()
        return t, t

    def surface(self, turns, date, scope=None):
        return self._all()

    def session_start(self, scope, host, first_message=None):
        return self._all()


class Oracle(System):
    name = "oracle"

    def _evidence(self, probe: dict | None = None) -> str:
        sess = {s["session_id"]: s for s in self.item["history"]["sessions"]}
        lines = []
        probes = [probe] if probe else self.item.get("probes") or []
        for p in probes:
            for ev in ((p or {}).get("gold") or {}).get("evidence") or []:
                s = sess.get(ev["session_id"])
                if s:
                    m = s["messages"][ev["message_index"]]
                    lines.append(f"[{str(s['date'])[:10]} {s['session_id']}] {m['role']}: {m['content']}")
        return ("<evidence>\n" + "\n".join(dict.fromkeys(lines)) + "\n</evidence>") if lines else ""

    def search(self, query, k=MEM_TOP_K, scope=None):
        probe = next((p for p in self.item.get("probes") or [] if p.get("query") == query), None)
        t = self._evidence(probe)
        return t, t

    def surface(self, turns, date, scope=None):
        return self._evidence()

    def session_start(self, scope, host, first_message=None):
        return self._evidence()


class NaiveRAGSystem(System):
    name = "naive_rag"
    tools = {"archive_search", "archive_read"}

    def __init__(self, embedder, threshold: float | None = None):
        self.embedder = embedder
        self.threshold = threshold
        if threshold is not None:
            self.name = "naive_rag_threshold"

    def setup(self, item, mode):
        from naive_rag import NaiveRAG, chunks_from_sessions

        self.item = item
        sessions = [{"session_id": s["session_id"], "date": str(s["date"])[:10],
                     "turns": [{"role": m["role"], "content": m["content"]} for m in s["messages"]]}
                    for s in item["history"]["sessions"]]
        with native_lock:
            self.rag = NaiveRAG(self.embedder, chunks_from_sessions(sessions))

    def _block(self, query: str, threshold: float | None = None) -> str:
        from naive_rag import max_dense, render_raw_block

        with native_lock:
            hits = self.rag.search(query, k=RAW_TOP_K)
        if threshold is not None and max_dense(hits) < threshold:
            return ""
        return render_raw_block(hits, BUDGET)

    def search(self, query, k=MEM_TOP_K, scope=None):
        t = self._block(query)
        return t, t

    def archive_search(self, query):
        return self._block(query) or "（没有检索到相关原文）"

    def archive_read(self, session_id):
        for s in self.item["history"]["sessions"]:
            if s["session_id"] == session_id:
                return render_session(s)
        return f"错误：没有会话 {session_id}"

    def surface(self, turns, date, scope=None):
        return self._block(turns[-1], self.threshold)

    def session_start(self, scope, host, first_message=None):
        return self._block(first_message) if first_message else ""


# ---------------------------------------------------------------- agent-memory


def load_agent_memory(root: Path):
    """把指定代码根目录放到 sys.path 最前，保证 import 到的是这一版 agent_memory（每个进程只加载一版）。"""
    sys.path.insert(0, str(root))
    for mod in [m for m in list(sys.modules) if m == "agent_memory" or m.startswith("agent_memory.")]:
        del sys.modules[mod]
    import agent_memory  # noqa: F401

    loaded = Path(sys.modules["agent_memory"].__file__).resolve().parents[1]
    if loaded != root.resolve():
        raise RuntimeError(f"agent_memory 加载自 {loaded}，不是 --am-root 指定的 {root}")
    return loaded


def extract_guidance(skill_md: Path) -> str:
    """从被测版本自己的 SKILL.md 摘出接入规范：首要原则段 + 与本评测相关的章节原文。

    章节按标题关键词选取（何时检索 / 回溯补全 / 复述确认 / 主动联想 / 工作记忆），
    两个版本用同一套规则提取，不为任何一方手写提示词。
    """
    text = skill_md.read_text(encoding="utf-8")
    parts = []
    m = re.search(r"\*\*首要原则.*?(?=\n## )", text, re.S)
    if m:
        parts.append(m.group(0).strip())
    for sec in re.split(r"\n(?=## )", text):
        title = sec.split("\n", 1)[0]
        if any(k in title for k in ("何时检索", "回溯", "复述确认", "主动联想", "完整性")):
            parts.append(sec.strip())
    return "\n\n".join(parts)


class _OracleLLM:
    """预置灌库时对账用：带 supersedes 的判 UPDATE，其余 ADD（与 e2e_eval 的规则一致，隔离沉淀质量）。"""

    def __init__(self, oracle: dict[str, dict]):
        self.oracle = oracle

    def complete(self, system, user):
        raise RuntimeError("预置灌库不跑蒸馏")

    def complete_json(self, system, user, schema_description):
        m = re.search(r"新记忆候选：\n- id: (\S+)", user)
        cid = m.group(1) if m else ""
        return self.oracle.get(cid, {"action": "ADD", "target_id": None, "reason": "oracle 默认"})


HOOK_SYSTEM = """[agent-memory 强制记忆更新] 你是宿主 agent。请根据最近几轮对话和当前工作记忆，给出更新后的完整工作记忆
（全量替换：没写的字段会被清空，所以要把仍然有效的旧内容一并带上）。字段：goal（当前任务目标）、decisions（已确认决策）、
variables（关键变量，键值都是文本）、todos（[{content, status}]，status 为 pending 或 done）、notes（未决问题与阻塞）。
只记录与当前任务相关的状态，与任务无关的闲聊不要写进去。"""
HOOK_SCHEMA = '{"goal": "...", "decisions": ["..."], "variables": {"k": "v"}, "todos": [{"content": "...", "status": "pending"}], "notes": ["..."]}'


class AgentMemorySystem(System):
    """agent-memory 适配器：preloaded（预置记忆，隔离沉淀质量）或 pipeline（真实写路径）两种灌库方式。"""

    def __init__(self, root: Path, label: str, embedder, settings_base, system_llm, hook_interval: int = 3,
                 retrieve_always: bool = False, disable: set[str] | None = None):
        self.root = root
        self.disable = set(disable or ())
        self.name = label + ("_retrieve_always" if retrieve_always else "") + (
            "-no_" + "_".join(sorted(self.disable)) if self.disable else "")
        self.embedder = embedder
        self.settings_base = settings_base
        self.system_llm = system_llm
        self.hook_interval = hook_interval
        self.retrieve_always = retrieve_always
        from agent_memory.server.mcp_server import MemoryService

        self._svc_cls = MemoryService
        # 用 hasattr 探测（新能力可能定义在混入基类上，不在 MemoryService.__dict__ 里）
        self.caps = {k for k in ("archive_search", "archive_read", "surface", "confirm_enqueue", "wm_refresh",
                                 "annotate_completeness", "episode_pack", "archive_sync")
                     if callable(getattr(MemoryService, k, None))}
        # 消融（A 轨）：--am-disable 去掉指定能力，看对应能力指标掉多少（suite-design §7）
        self.caps -= self.disable
        tools = {"memory_search"}
        if "archive_search" in self.caps:
            tools |= {"archive_search", "archive_read"}
        if "confirm_enqueue" in self.caps:
            tools.add("queue_confirmation")
        tools.add("state_read")
        self.tools = tools
        skill = root / "skills" / "agent-memory" / "SKILL.md"
        self._guidance = extract_guidance(skill) if skill.exists() else ""

    def guidance(self) -> str:
        return self._guidance

    # ---- 灌库
    def setup(self, item: dict, mode: str) -> None:
        from agent_memory.long_term.store.index_db import IndexDB
        from agent_memory.long_term.store.markdown_store import MarkdownStore

        self.item = item
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        data = Path(self.tmp.name)
        self.settings = self.settings_base.model_copy(update={"data_dir": data, "review_gate": "off"})
        # 每题一个计数器：被测系统内部 LLM 的调用量（Q1 成本）
        self.meter = CostMeter()
        llm = MeteredLLM(self.system_llm, self.meter) if self.system_llm is not None else None
        with native_lock:
            self.store = MarkdownStore(data)
            self.index = IndexDB(data / "index.db")
            self.svc = self._svc_cls(self.settings, self.store, self.index, self.embedder, llm)
        self.offsets: dict[str, int] = {}
        if mode == "preloaded":
            self._preload(item)
        elif mode == "pipeline":
            self._pipeline(item)

    def _archive(self, s: dict) -> int:
        recs = [{"role": m["role"], "content": m["content"]} for m in s["messages"]]
        kw = {}
        if "archive_sync" in self.caps:  # 优化版：带日期、宿主、作用域的归档（写入即索引）
            kw = {"session_date": str(s["date"]), "scope": scope_of(s), "host": s.get("host")}
            res = self.svc.archive_records(recs, source="eval", session_id=s["session_id"], **kw)
            return res["offset"]
        with native_lock:
            _path, off = self.svc._archive_raw(recs, "eval", s["session_id"])
        return off

    def _preload(self, item: dict) -> None:
        from agent_memory.long_term.ingest.gate import gate_candidates
        from agent_memory.long_term.ingest.reconcile import reconcile
        from agent_memory.models import EvidenceRef, MemoryEntry

        sess = {s["session_id"]: s for s in item["history"]["sessions"]}
        for s in item["history"]["sessions"]:
            self.offsets[s["session_id"]] = self._archive(s)
        mems = item["history"].get("preloaded_memories") or []
        oracle = {m["id"]: ({"action": "UPDATE", "target_id": m["supersedes"], "reason": "supersedes"}
                            if m.get("supersedes") else {"action": "ADD", "target_id": None, "reason": "preset"}) for m in mems}
        by_session: dict[str, list] = {}
        for m in mems:
            s = sess[m["source_session"]]
            created = session_date(s)
            off = self.offsets.get(s["session_id"], 0)
            fields = dict(
                id=m["id"], content=m["content"], memory_type=m["memory_type"], scope=scope_of(s),
                confidence=m.get("confidence", "high"), source="eval",
                evidence=[EvidenceRef(session_id=s["session_id"], source="eval",
                                      line_range=(off + 1, off + len(s["messages"])))],
                created_at=created, last_verified=dt.date.fromisoformat(str(m.get("last_verified") or created)),
            )
            by_session.setdefault(s["session_id"], []).append(MemoryEntry(**fields))
        written = []
        for s in item["history"]["sessions"]:
            cands = by_session.get(s["session_id"]) or []
            if not cands:
                continue
            # 不能在这里持有 native_lock：reconcile 内部开线程池并发找近邻，工作线程里的嵌入调用
            # 会再去拿同一把锁而死锁。嵌入的原生调用已由 CachedEmbedder 串行化。
            gate = gate_candidates(cands, Path(self.tmp.name))
            reconcile(gate.passed, self.store, self.index, _OracleLLM(oracle), embedder=self.embedder,
                      settings=self.settings)
            written += [c.id for c in gate.passed]
        if "annotate_completeness" in self.caps and self.system_llm is not None:
            # 优化版的 P27/P13：写入后对照原文做完整度与一致性核验（与生产路径同一函数）
            self.svc.annotate_completeness(written)

    def _pipeline(self, item: dict) -> None:
        import inspect

        sig = inspect.signature(self.svc.add).parameters
        for s in item["history"]["sessions"]:
            conv = json.dumps([{"role": m["role"], "content": m["content"]} for m in s["messages"]], ensure_ascii=False)
            kw = {"conversation_json": conv, "scope": scope_of(s), "source": "eval", "session_id": s["session_id"]}
            if "session_date" in sig:
                kw["session_date"] = str(s["date"])
            if "host" in sig:
                kw["host"] = s.get("host")
            self.add_reports = getattr(self, "add_reports", []) + [self.svc.add(**kw)]

    # ---- 读
    def search(self, query, k=MEM_TOP_K, scope=None):
        scope = scope or target_scope(self.item)
        with native_lock:
            r = self.svc.search(query, scope=scope, k=k, acknowledge_pending=True)
        raw = (r.get("block") or "") + "\n" + "\n".join(
            (h.get("content") or "") + " " + (h.get("detail") or "") for h in r.get("hits") or [])
        raw += "\n" + "\n".join(x.get("text", "") for x in r.get("archive_hits") or [])
        return r.get("block") or "", raw

    def archive_search(self, query):
        if "archive_search" not in self.caps:
            return "（不可用）"
        with native_lock:
            r = self.svc.archive_search(query, scope=target_scope(self.item), k=RAW_TOP_K)
        return r.get("block") or "（没有检索到相关原文）"

    def archive_read(self, session_id):
        if "archive_read" not in self.caps:
            return "（不可用）"
        # 渲染出的出处写作 "eval/s1"，答题器常原样传回来：按 "来源/会话" 拆开
        source, _, sid = session_id.strip().rpartition("/")
        try:
            r = self.svc.archive_read(source=source or "eval", session_id=sid)
        except ValueError as e:
            return f"错误：{e}"
        return r.get("text") or f"错误：没有会话 {session_id}"

    def surface(self, turns, date, scope=None):
        scope = scope or target_scope(self.item)
        if self.retrieve_always:
            return self.search(turns[-1], scope=scope)[0]
        if "surface" not in self.caps:
            return ""  # 基线没有主动浮现机制：没人问就不注入
        r = self.svc.surface(turns[-1], scope=scope, recent_turns=turns[:-1], date=date)
        return r.get("block") or ""

    def session_start(self, scope, host, first_message=None):
        blocks = []
        for sc in dict.fromkeys(["global", scope]):
            b = self.svc.wm_read(sc)["block"]
            if b:
                blocks.append(b)
        if first_message and "surface" in self.caps:
            b = self.svc.surface(first_message, scope=scope, recent_turns=[], date=None).get("block")
            if b:
                blocks.append(b)
        return "\n\n".join(blocks)

    def queue(self, message):
        if "confirm_enqueue" not in self.caps:
            return "错误：没有待确认队列"
        r = self.svc.confirm_enqueue(message=message, scope=target_scope(self.item))
        return f"已写入待确认队列（{r.get('id')}）。"

    def state_read(self, scope, task=None):
        b = self.svc.wm_read(scope)["block"]
        return b or "（没有工作记忆）"

    def wm_snapshot(self, scope):
        return self.svc.wm_read(scope)["block"]

    # ---- 会话进行中的宿主事件（ts / pf 用）
    def observe(self, messages, event, scope, host_llm=None, turn_index=0):
        """event: turn（每条用户消息后）/ compaction / session_end / interrupt / task_switch。

        基线：按 SKILL 的强制更新 hook，每 N 轮由宿主（答题器同款模型）改写工作记忆；
              session_end 时调 memory_session_end 归档 + 蒸馏。
        优化版：同样的节奏改为服务端 wm_refresh（P08）；压缩前做 episode_pack（P05/P06）；
              会话消息持续归档（P02），当前会话也能被原文检索到。
        """
        conv = [{"role": m["role"], "content": m["content"]} for m in messages]
        live_id = "live-" + re.sub(r"[^A-Za-z0-9._-]+", "-", scope)  # 会话 id 只允许字母数字点下划线连字符
        if "archive_sync" in self.caps and event in {"turn", "compaction", "session_end"}:
            self.svc.archive_records(conv[self._synced(scope):], source="eval", session_id=live_id,
                                     session_date=None, scope=scope, host=None)
            self._sync_mark[scope] = len(conv)
        due = event != "turn" or (turn_index and turn_index % self.hook_interval == 0)
        if due:
            recent = conv[-2 * self.hook_interval:]
            if "wm_refresh" in self.caps:
                self.svc.wm_refresh(scope, conversation=recent, current_turn=turn_index)
            elif host_llm is not None:
                cur = self.svc.wm_read(scope)
                # 去掉写入时间戳：它每次运行都不同，会让宿主提示词无法命中评测缓存，重跑结果不可复现
                wm = {k: v for k, v in (cur["working_memory"] or {}).items() if k != "updated_at"}
                user = ("当前工作记忆：\n" + (json.dumps(wm, ensure_ascii=False) if cur["exists"] else "（空）")
                        + "\n\n最近几轮对话：\n" + "\n".join(f"{m['role']}: {m['content']}" for m in recent))
                try:
                    out = host_llm.complete_json(HOOK_SYSTEM, user, HOOK_SCHEMA)
                    self.svc.wm_write(scope, goal=out.get("goal") or None, decisions=out.get("decisions") or [],
                                      variables={str(k): str(v) for k, v in (out.get("variables") or {}).items()},
                                      todos=out.get("todos") or [], notes=out.get("notes") or [],
                                      turn_watermark=turn_index)
                except Exception:  # noqa: BLE001  宿主 hook fail-open
                    pass
        if event == "compaction" and "episode_pack" in self.caps:
            self.svc.episode_pack(conv, scope=scope, session_id=f"live-{scope}", reason="compaction")
        if event == "session_end":
            try:
                self.svc.session_end(scope, conversation_json=conv, source="eval",
                                     session_id=f"end-{scope}-{turn_index}", force=True)
            except Exception:  # noqa: BLE001
                pass

    _sync_mark: dict = {}

    def _synced(self, scope):
        if not isinstance(self._sync_mark, dict) or self._sync_mark is AgentMemorySystem._sync_mark:
            self._sync_mark = {}
        return self._sync_mark.get(scope, 0)

    def close(self):
        try:
            with native_lock:
                self.index.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()


def now_date(item: dict) -> str:
    refs = [p.get("reference_time") for p in item.get("probes") or [] if p.get("reference_time")]
    if refs:
        return str(parse_time(refs[0]).date())
    trig = ((item.get("behavior") or {}).get("trigger") or {})
    return str(trig.get("date") or session_date(item["history"]["sessions"][-1]))
