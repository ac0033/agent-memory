"""MCP Server：把记忆内核暴露为十五个 MCP tool，stdio 传输。

长期记忆 tool（八个）：
- memory_search：混合检索 + render_recall_block 渲染，scope 过滤在检索层强制
  （只查调用方给的 scope + global，global 由 HybridSearcher 自动并入）。
  M5 复核门：复核队列有积压时按 settings.review_gate 处置——ask 档拦截并等
  用户确认（acknowledge_pending=true 放行），strict 档一律拒读，off 不拦；
- memory_add：conversation_json 走 归档→蒸馏→评价门→对账 全管线；distilled_json
  走宿主蒸馏（M9，已有 raw 证据才自动对账，否则转人工复核）；
  单条 content 走 脱敏→评价门→对账。返回各阶段报告 + pending_review 待复核
  明细（M5）。失败语义（M8）：评价门拒绝是确定性拦截（报错含命中片段，
  原样重试无效；force_review=true 转人工复核）；LLM 失败是临时性故障
  （原文已归档 data/raw，status=archived_only，可重试）；
- memory_distill_prompt（M9）：返回宿主蒸馏协议（system prompt + 输出 schema +
  使用说明），供无 API key 的订阅制 agent 自行蒸馏后走 distilled_json 提交；
- memory_feedback：按反馈升降 confidence；降到 low 以下移出正式库、进复核队列；
- memory_update：过脱敏 + 评价门后更新；
- memory_forget：删除；
- memory_review_list（M5）：列出复核队列待办明细；
- memory_review_resolve（M5）：人工裁决待办（approve 入库 / discard 丢弃 /
  modify 改文本后入库），裁决后删除队列文件。

工作记忆与统一上下文 tool（M7a，四个）：
- memory_wm_read / memory_wm_write / memory_wm_clear：工作记忆（操作层，
  当前任务状态）的读 / 全量写 / 清空。写入只过脱敏，不过评价门、不做对账
  （评价门的祈使句拦截与 TODO 天然冲突）；
- memory_context：统一上下文组装——常驻画像块 + 工作记忆块 + 召回块，
  按框架顺序拼接；复核门语义与 memory_search 一致（blocked 原样透出）。

短期记忆 tool（M7b，两个）：
- memory_transcript_read：把 agent 会话日志（如 kimi-code 的 wire.jsonl）
  解析成干净轮次序列（user/assistant/tool），纯读不写；since_turn 配合
  工作记忆水位做新鲜度补偿的增量读取（只返回水位之后的轮次）；
- memory_session_end：会话结束收尾编排——归档原文（data/raw，只追加不改写）
  + 联合蒸馏（对话 + 工作记忆快照作参考上下文）+ 已完成 TODO 清理；
  工作记忆有未完成任务时 veto 不视为结束（确认结束传 force=true）。

启动：uv run python -m agent_memory.server.mcp_server
组件构建在 main() 里完成；MemoryService 是纯 Python 类，测试直接注入
fake embedder / fake LLM / fake working store 调用，不走 MCP 传输。
"""

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agent_memory.config import Settings, get_settings
from agent_memory.io_utils import interprocess_lock
from agent_memory.llm import LLMClient, LLMError, OpenAILLMClient, ValidatingLLMClient
from agent_memory.long_term.ingest.distill import (
    build_entries_from_distilled,
    distill_memories,
    get_distill_protocol,
    parse_conversation_json,
)
from agent_memory.long_term.ingest.gate import gate_candidates, write_review_queue
from agent_memory.long_term.ingest.reconcile import reconcile
from agent_memory.long_term.ingest.redact import redact
from agent_memory.long_term.ingest.review_queue import (
    delete_review_item,
    list_review_queue,
    load_review_item,
    review_queue_lock,
)
from agent_memory.long_term.retrieve.embedder import get_embedder
from agent_memory.long_term.retrieve.hybrid import HybridSearcher
from agent_memory.long_term.retrieve.inject import render_recall_block
from agent_memory.long_term.retrieve.resident import build_system_context
from agent_memory.long_term.store.coordinator import MemoryWriter
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore, MemoryStoreError
from agent_memory.models import MemoryEntry, is_valid_scope, normalize_scope, validate_entry_id
from agent_memory.short_term.adapter import detect_adapter, get_adapter
from agent_memory.working.models import TodoItem, WorkingMemory
from agent_memory.working.render import is_stale, render_working_memory_block
from agent_memory.working.store import WorkingMemoryNotFoundError, WorkingMemoryStore

# confidence 的升降阶梯：feedback 沿它走一步
_CONFIDENCE_LADDER = ["low", "medium", "high"]

# session_end 把工作记忆快照渲染给蒸馏当参考上下文的预算：蒸馏材料不进注入层，
# 给宽松预算（注入层 working_memory_budget_chars 的数倍），避免快照被预算挤丢
_SESSION_END_WM_SNAPSHOT_BUDGET_CHARS = 4000
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass
class AddReport:
    """memory_add 的分阶段报告。"""

    mode: str  # "distill"（对话蒸馏）| "distilled"（宿主蒸馏）| "direct"（单条内容）
    # M8 写入结果状态：ok 正常入库；archived_only 原文已归档但蒸馏未执行
    # （LLM 未配置/调用失败，内容不丢，可稍后重试）；queued_for_review
    # 评价门拒绝后按 force_review=true 转人工复核
    status: str = "ok"
    archive_path: str | None = None  # 原文归档位置（data/raw/...）
    warning: str | None = None  # 非 ok 状态的原因与建议的下一步
    distilled: int = 0
    normalized_ids: dict[str, str] = field(default_factory=dict)  # id 规范化：原 -> 新
    queued_invalid: int = 0  # 规范化后仍非法、进复核队列的条数
    dropped_redacted: int = 0  # 脱敏后无实质内容被丢弃的条数（唯一保留的丢弃路径）
    gate_rejected: list[dict] = field(default_factory=list)
    gate_queued: list[str] = field(default_factory=list)
    reconcile: dict[str, int] = field(default_factory=dict)
    # M5：本次写入产生的待复核明细（id / content_preview / reason），
    # 供 agent 在回复中逐条向用户报告并请其裁决
    pending_review: list[dict] = field(default_factory=list)
    # M6：scope 缺省时的作用域提醒（显式传了 scope 则为 None）
    scope_reminder: str | None = None


# LLM 调用类失败（临时性故障，区别于评价门这种确定性失败）：蒸馏/对账阶段
# 抛这些异常时 memory_add 归档原文后降级 archived_only，而不是让内容丢失
try:
    from openai import APIError as _OpenAIAPIError

    _LLM_CALL_ERRORS: tuple[type[Exception], ...] = (LLMError, _OpenAIAPIError)
except ImportError:  # pragma: no cover - openai 是硬依赖，防御性兜底
    _LLM_CALL_ERRORS = (LLMError,)


_SCOPE_REMINDER = (
    "本次写入使用了默认 scope=global（全局共享，对所有项目可见）。"
    "若该记忆只与某个项目或某个 agent 相关，应显式指定 scope 为 "
    "repo:<项目名> 或 agent:<名字>；写错了可 memory_forget 后按正确 scope 重存。"
)

# 写类 tool 描述统一追加的 subagent 约束。工具描述是 agent 中立的提示词通道：
# 任何宿主派生的 subagent，只要工具面里有这个 tool 就会看到这句。它是提示层
# 兜底，真正的硬闸在宿主的 subagent 工具配置（如 kimi-code 的 disallowedTools，
# 见 agents/coder.md）；服务端无法区分主 agent 与 subagent（共用同一 MCP
# 连接），所以不在服务端做按调用方降级
_SUBAGENT_WRITE_GUARD = (
    "仅限主 agent 调用：subagent 禁止使用本工具——记忆库对 subagent 只读，"
    "值得跨会话沉淀的结论请写进你的最终回复，由主 agent 决定是否入库。"
)


def _pending_from_entry(entry: MemoryEntry, reason: str) -> dict:
    return {"id": entry.id, "content_preview": entry.content[:80], "reason": reason}


def _pending_from_raw(raw: object, reason: str) -> dict:
    rid = raw.get("id") if isinstance(raw, dict) else None
    content = raw.get("content") if isinstance(raw, dict) else str(raw)
    return {"id": rid, "content_preview": str(content)[:80], "reason": reason}


def _validate_archive_segment(value: str, field_name: str) -> str:
    if not _SAFE_PATH_SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(
            f"{field_name} 非法：只能使用 1-128 位字母、数字、点、下划线或连字符，"
            "且不能包含路径分隔或上级目录"
        )
    return value


class MemoryService:
    """十五个 MCP tool 的业务实现。与传输解耦，测试直接实例化调用。"""

    def __init__(
        self,
        settings: Settings,
        store: MarkdownStore,
        index: IndexDB,
        embedder,
        llm: LLMClient | None = None,
        working_store: WorkingMemoryStore | None = None,
    ):
        self.settings = settings
        self.store = store
        self.index = index
        self.embedder = embedder
        self.llm = ValidatingLLMClient(llm) if llm is not None else None
        # 工作记忆存储缺省按 settings.data_dir 自建；测试可注入以隔离
        self.working_store = working_store or WorkingMemoryStore(settings.data_dir)
        self.searcher = HybridSearcher(store, index, embedder, settings)
        self.writer = MemoryWriter(store, index, embedder)

    def consistency(self) -> dict[str, Any]:
        """只读检查 Markdown 正式层与派生索引是否一致。"""
        report = self.writer.check_consistency()
        return {
            "status": "ok" if report.consistent else "inconsistent",
            "markdown_only": list(report.markdown_only),
            "index_only": list(report.index_only),
            "mismatched": list(report.mismatched),
        }

    def _review_gate_block(
        self, acknowledge_pending: bool
    ) -> tuple[int, dict[str, Any] | None]:
        pending_count = len(list_review_queue(self.settings.data_dir))
        gate = self.settings.review_gate
        if pending_count and gate == "strict":
            return pending_count, {
                "status": "blocked",
                "gate": gate,
                "pending_review_count": pending_count,
                "message": (
                    f"记忆库有 {pending_count} 条人工复核尚未确认，当前 review_gate=strict"
                    "（本环境不做交互确认），暂未读取记忆。请先通过 memory_review_list /"
                    " memory_review_resolve 处理待办，或将 review_gate 调低一档。"
                ),
            }
        if pending_count and gate == "ask" and not acknowledge_pending:
            return pending_count, {
                "status": "blocked",
                "gate": gate,
                "pending_review_count": pending_count,
                "message": (
                    f"记忆库有 {pending_count} 条人工复核尚未确认。请先向用户确认："
                    "现在逐条处理（memory_review_list 查看 + memory_review_resolve 裁决），"
                    "还是本次照常读取（用户明确同意后以 acknowledge_pending=true 重试本调用）。"
                ),
            }
        return pending_count, None

    # ---- memory_search ----

    def search(
        self, query: str, scope: str = "global", k: int = 5, acknowledge_pending: bool = False
    ) -> dict[str, Any]:
        """混合检索并渲染注入块。scope 过滤在检索层强制：只查 scope + global。

        复核门（M5）：复核队列有积压时按 settings.review_gate 处置——
        ask：返回 blocked，等用户确认后带 acknowledge_pending=true 重试；
        strict：一律拒读（acknowledge_pending 也不放行），直到队列清空；
        off：不拦。返回里的 pending_review_count 供 agent 向用户告知队列状态。
        """
        # scope 归一化与写路径同口径（下划线等旧写法折叠为连字符）；
        # 归一化后仍非法的当场报错，绝不静默返回空结果
        scope = normalize_scope(scope)
        if not is_valid_scope(scope):
            raise ValueError(
                "scope 非法：必须匹配 global | repo:<slug> | agent:<name>"
                f"（slug 为小写字母/数字/连字符），收到: {scope!r}"
            )
        pending_count, blocked = self._review_gate_block(acknowledge_pending)
        if blocked:
            return blocked
        results = self.searcher.search(query, scopes=[scope], k=k, track_retrieval=True)
        block = render_recall_block(results, self.settings.recall_budget_chars)
        hits = [
            {
                "id": r.entry.id,
                "content": r.entry.content,
                "detail": r.entry.detail,
                "scope": r.entry.scope,
                "memory_type": r.entry.memory_type,
                "confidence": r.entry.confidence,
                "last_verified": r.entry.last_verified.isoformat(),
                "score": round(r.score, 6),
            }
            for r in results
        ]
        return {
            "status": "ok",
            "pending_review_count": pending_count,
            "block": block,
            "hits": hits,
        }

    # ---- memory_add ----

    def add(
        self,
        content: str | None = None,
        conversation_json: str | list | None = None,
        distilled_json: str | None = None,
        scope: str | None = None,
        source: str = "mcp",
        session_id: str | None = None,
        entry_id: str | None = None,
        memory_type: str = "semantic",
        confidence: str = "high",
        force_review: bool = False,
    ) -> dict[str, Any]:
        """写入记忆。conversation_json > distilled_json > content 三选一。

        conversation_json 推荐传 [{role, content}, ...] 的 JSON 字符串；直接传
        list 也可以（服务端自动序列化）。其他类型报 ValueError 并提示正确格式。

        distilled_json 是宿主蒸馏模式（M9）：宿主 agent 自己完成蒸馏后，把
        {"memories": [...]} 的 JSON 字符串提交进来，候选照常过 校验/规范化→
        脱敏→评价门；source/session_id 和 evidence_turns 能指向已有 raw 证据时
        才进入对账，否则转人工复核。

        scope 应显式选择（M6 作用域纪律）：global 放跨项目通用知识，repo:<项目名>
        放项目相关，agent:<名字> 放 agent 自身相关。缺省回落 global 并附提醒。

        失败语义（M8）：评价门拒绝是确定性规则拦截——原样重试结果不变，报错
        会说明命中原因；force_review=true 时被拒条目不丢弃，转人工复核队列。
        LLM 调用失败是临时性故障——对话原文会先归档到 data/raw（D1 只追加），
        返回 status=archived_only，稍后重试同一份 conversation_json 即可。
        """
        scope_reminder = None
        if scope is None:
            scope = "global"
            scope_reminder = _SCOPE_REMINDER
        else:
            # 与检索路径同口径归一化（repo:llm_wiki -> repo:llm-wiki）；
            # 归一化后仍非法的由 MemoryEntry 校验 fail-closed
            scope = normalize_scope(scope)
        if isinstance(conversation_json, list):
            # 容错：调用方把对话作为原生数组而非 JSON 字符串传来（LLM 调工具的
            # 常见错误），能解析就代为序列化；空数组按未提供处理
            conversation_json = (
                json.dumps(conversation_json, ensure_ascii=False) if conversation_json else None
            )
        elif conversation_json is not None and not isinstance(conversation_json, str):
            raise ValueError(
                "conversation_json 格式错误：请传 [{role, content}, ...] 的 JSON "
                f"字符串（或等价的数组），收到的是 {type(conversation_json).__name__}"
            )
        if conversation_json:
            report = self._add_conversation(
                conversation_json, scope, source, session_id, force_review=force_review
            )
        elif distilled_json:
            report = self._add_distilled(
                distilled_json, scope, source, session_id, force_review=force_review
            )
        else:
            if content is None:
                raise ValueError(
                    "memory_add 需要 content、conversation_json 或 distilled_json 之一"
                )
            report = self._add_direct(
                content, scope, source, entry_id, memory_type, confidence, force_review
            )
        report.scope_reminder = scope_reminder
        return asdict(report)

    def _archive_raw(self, records: list[dict], source: str, session_id: str) -> tuple[Path, int]:
        """把原始材料追加归档到 data/raw/<source>/<session_id>.jsonl。

        D1 只追加不改写：同 session_id 调两次是追加不是覆盖。
        """
        source = _validate_archive_segment(source, "source")
        session_id = _validate_archive_segment(session_id, "session_id")
        archive_path = self.settings.data_dir / "raw" / source / f"{session_id}.jsonl"
        with interprocess_lock(self.settings.data_dir / "state" / "raw_archive.lock"):
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            offset = 0
            if archive_path.exists():
                with archive_path.open("r", encoding="utf-8") as existing:
                    offset = sum(1 for _ in existing)
            with archive_path.open("a", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return archive_path, offset

    def _add_conversation(
        self,
        conversation_json: str,
        scope: str,
        source: str,
        session_id: str | None,
        extra_context: str | None = None,
        force_review: bool = False,
        archive: bool = True,
        evidence_line_offset: int = 0,
        evidence_line_map: list[int] | None = None,
    ) -> AddReport:
        # 先解析校验（非法对话不归档——不把垃圾写进 data/raw），再归档、再蒸馏：
        # 归档在 LLM 调用之前，LLM 不可用/超时时内容不丢，可事后重放
        conversation = parse_conversation_json(conversation_json)
        session_id = session_id or f"session-{datetime.now():%Y%m%dT%H%M%S}"
        if archive:
            archive_file, evidence_line_offset = self._archive_raw(conversation, source, session_id)
            archive_path = str(archive_file)
        else:
            archive_path = None
        if self.llm is None:
            return AddReport(
                mode="distill",
                status="archived_only",
                archive_path=archive_path,
                warning=(
                    "未配置 LLM（AGENT_MEMORY_LLM_API_KEY），对话原文已归档、未蒸馏；"
                    "配置 LLM 后重试同一份 conversation_json 即可。无 API key 的宿主"
                    "（订阅制 agent）可改走宿主蒸馏：调 memory_distill_prompt 拿蒸馏协议，"
                    "自行蒸馏后以 distilled_json 参数重新提交"
                ),
            )
        try:
            distill_result = distill_memories(
                conversation, scope, source, session_id, self.llm,
                data_dir=self.settings.data_dir,
                extra_context=extra_context,
                evidence_line_offset=evidence_line_offset,
                evidence_line_map=evidence_line_map,
            )
            gate_result = gate_candidates(distill_result.entries, self.settings.data_dir)
            report = reconcile(
                gate_result.passed, self.store, self.index, self.llm,
                embedder=self.embedder, settings=self.settings,
            )
        except _LLM_CALL_ERRORS as e:
            # 临时性故障（超时/限流/解析失败）：原文已归档不丢，明确告知可重试
            return AddReport(
                mode="distill",
                status="archived_only",
                archive_path=archive_path,
                warning=(
                    f"LLM 调用失败（{type(e).__name__}: {e}）。这是临时性故障："
                    "对话原文已归档不会丢失，稍后重试同一份 conversation_json 即可"
                ),
            )
        return self._distill_report(
            "distill", archive_path, distill_result, gate_result, report, force_review
        )

    def _add_distilled(
        self,
        distilled_json: str,
        scope: str,
        source: str,
        session_id: str | None,
        force_review: bool = False,
    ) -> AddReport:
        """宿主蒸馏模式（M9）：宿主 agent 自己蒸馏，服务端只对候选过门。

        与服务端蒸馏共用 build_entries_from_distilled（校验/规范化→脱敏），
        之后照常过评价门。source/session_id 指向服务端已有 raw 归档且
        evidence_turns 有效的候选可进入对账；没有可核查证据的候选进入人工复核。
        """
        try:
            parsed = json.loads(distilled_json)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"distilled_json 不是合法 JSON（{e}）；"
                '期望格式：{"memories": [...]} 的 JSON 字符串，'
                "结构见 memory_distill_prompt 返回的 schema_description"
            ) from e
        if not isinstance(parsed, dict):
            raise ValueError(
                'distilled_json 必须是 {"memories": [...]} 的 JSON object 字符串，'
                f"收到的是 {type(parsed).__name__}"
            )
        source = _validate_archive_segment(source, "source")
        session_id = _validate_archive_segment(
            session_id or f"session-{datetime.now():%Y%m%dT%H%M%S}", "session_id"
        )
        archive_file = self.settings.data_dir / "raw" / source / f"{session_id}.jsonl"
        archived_turns: int | None = None
        valid_evidence_lines: set[int] | None = None
        with interprocess_lock(self.settings.data_dir / "state" / "raw_archive.lock"):
            if archive_file.exists():
                lines = archive_file.read_text(encoding="utf-8").splitlines()
                valid_evidence_lines = set()
                for line_number, line in enumerate(lines, start=1):
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"raw 证据文件第 {line_number} 行不是合法 JSON：{archive_file}"
                        ) from exc
                    if (
                        isinstance(record, dict)
                        and record.get("role") in {"user", "assistant"}
                        and isinstance(record.get("content"), str)
                        and record["content"].strip()
                    ):
                        valid_evidence_lines.add(line_number)
                archived_turns = len(lines)
        distill_result = build_entries_from_distilled(
            parsed, scope, source, session_id, n_turns=archived_turns,
            data_dir=self.settings.data_dir,
            strict_evidence=archived_turns is not None,
            valid_evidence_lines=valid_evidence_lines,
        )
        gate_result = gate_candidates(distill_result.entries, self.settings.data_dir)
        unverified = [entry for entry in gate_result.passed if not entry.evidence]
        if unverified:
            write_review_queue(
                unverified,
                self.settings.data_dir,
                reason="宿主蒸馏候选没有服务端原始证据，需人工确认后入库",
            )
        report = reconcile(
            [entry for entry in gate_result.passed if entry.evidence],
            self.store, self.index, self.llm,
            embedder=self.embedder, settings=self.settings,
        )
        report.queued.extend(
            (entry, "宿主蒸馏候选没有服务端原始证据，需人工确认")
            for entry in unverified
        )
        result = self._distill_report(
            "distilled", None, distill_result, gate_result, report, force_review
        )
        if unverified:
            result.warning = f"{len(unverified)} 条宿主蒸馏候选因缺少原始证据进入人工复核"
        return result

    def _distill_report(
        self,
        mode: str,
        archive_path: str | None,
        distill_result,
        gate_result,
        report,
        force_review: bool,
    ) -> AddReport:
        """distill / distilled 两种模式共用的报告组装（含 force_review 转复核）。"""
        # force_review：评价门拒绝的候选不丢弃，转人工复核队列裁决
        force_queued_files: list[Path] = []
        if force_review and gate_result.rejected:
            force_queued_files = write_review_queue(
                [e for e, _ in gate_result.rejected],
                self.settings.data_dir,
                reason="评价门拒绝，按 force_review=true 转人工复核",
            )
        return AddReport(
            mode=mode,
            archive_path=archive_path,
            distilled=len(distill_result.entries),
            normalized_ids=distill_result.normalized_ids,
            queued_invalid=len(distill_result.invalid_records),
            dropped_redacted=distill_result.dropped_redacted,
            gate_rejected=[
                {"id": e.id, "reason": reason} for e, reason in gate_result.rejected
            ],
            gate_queued=[e.id for e in gate_result.queued],
            reconcile=report.counts(),
            warning=(
                f"{len(force_queued_files)} 条被评价门拒绝的候选已转人工复核队列"
                if force_queued_files else None
            ),
            pending_review=(
                [
                    _pending_from_raw(raw, f"蒸馏产出非法：{item_reason}")
                    for raw, item_reason in distill_result.invalid_records
                ]
                + [
                    _pending_from_entry(e, "confidence=low，规则门分流")
                    for e in gate_result.queued
                ]
                + (
                    [
                        _pending_from_entry(e, f"评价门拒绝（force_review 转复核）：{reason}")
                        for e, reason in gate_result.rejected
                    ]
                    if force_queued_files
                    else []
                )
                + [_pending_from_entry(e, reason) for e, reason in report.queued]
                + [
                    _pending_from_entry(self.store.get(entry_id), "变更传播判定需修订")
                    for p in report.propagation
                    for entry_id in p.needs_revision
                ]
                + [
                    _pending_from_entry(self.store.get(entry_id), reason)
                    for p in report.propagation
                    for entry_id, reason in p.failed
                ]
            ),
        )

    # ---- memory_distill_prompt ----

    def distill_protocol(self) -> dict[str, Any]:
        """返回宿主蒸馏协议（M9）：蒸馏 system prompt + 输出 schema + 使用说明。"""
        protocol = get_distill_protocol()
        protocol["usage"] = (
            "宿主蒸馏流程：1. 把要沉淀的对话按 conversation_format 渲染成带 turn "
            "编号的文本；2. 以 system_prompt 为指令、schema_description 为输出结构，"
            "在你自己的上下文里完成蒸馏（只沉淀用户明确确认过的内容）；3. 把产出的 "
            '{"memories": [...]} 以 JSON 字符串传给 memory_add 的 distilled_json '
            "参数提交。服务端会对候选做校验/规范化→脱敏→评价门；请传入已有"
            "raw 归档的 source/session_id，并让 evidence_turns 指向其中的有效对话行。"
            "缺少可核查证据的候选会进入人工复核。"
        )
        return protocol

    def _add_direct(
        self,
        content: str,
        scope: str,
        source: str,
        entry_id: str | None,
        memory_type: str,
        confidence: str,
        force_review: bool = False,
    ) -> AddReport:
        if not entry_id:
            raise ValueError("单条 content 写入必须提供 entry_id（kebab-case）")
        redacted, _hits = redact(content)
        today = date.today()
        entry = MemoryEntry(
            id=entry_id,
            content=redacted,
            memory_type=memory_type,  # type: ignore[arg-type]
            scope=scope,
            confidence=confidence,  # type: ignore[arg-type]
            source=source,
            created_at=today,
            last_verified=today,
        )
        gate_result = gate_candidates([entry], self.settings.data_dir)
        if gate_result.rejected:
            rejected_entry, reason = gate_result.rejected[0]
            if not force_review:
                # 确定性规则拦截：报错里已含命中片段与"原样重试无效"说明
                raise ValueError(f"评价门拒绝入库：{reason}")
            # force_review：拒绝即分流而非丢弃，交人工复核裁决
            write_review_queue(
                [rejected_entry],
                self.settings.data_dir,
                reason=f"评价门拒绝，按 force_review=true 转人工复核：{reason}",
            )
            return AddReport(
                mode="direct",
                status="queued_for_review",
                gate_rejected=[{"id": rejected_entry.id, "reason": reason}],
                warning=(
                    "评价门拒绝入库，已按 force_review=true 转人工复核队列；"
                    "请用 memory_review_list 查看、memory_review_resolve 裁决"
                ),
                pending_review=[
                    _pending_from_entry(
                        rejected_entry, f"评价门拒绝（force_review 转复核）：{reason}"
                    )
                ],
            )
        report = reconcile(
            gate_result.passed, self.store, self.index, self.llm,
            embedder=self.embedder, settings=self.settings,
        )
        return AddReport(
            mode="direct",
            gate_queued=[e.id for e in gate_result.queued],
            reconcile=report.counts(),
            pending_review=(
                [
                    _pending_from_entry(e, "confidence=low，规则门分流")
                    for e in gate_result.queued
                ]
                + [_pending_from_entry(e, reason) for e, reason in report.queued]
                + [
                    _pending_from_entry(self.store.get(entry_id), "变更传播判定需修订")
                    for p in report.propagation
                    for entry_id in p.needs_revision
                ]
                + [
                    _pending_from_entry(self.store.get(entry_id), reason)
                    for p in report.propagation
                    for entry_id, reason in p.failed
                ]
            ),
        )

    # ---- memory_feedback ----

    def feedback(self, memory_id: str, helpful: bool, note: str | None = None) -> dict[str, Any]:
        """反馈调整 confidence：helpful 升一档，不 helpful 降一档；
        已是 low 再降就移出正式库、写入复核队列。"""
        validate_entry_id(memory_id)
        with interprocess_lock(self.writer.lock_path):
            entry = self.store.get(memory_id)  # 锁内读取，避免覆盖并发正文更新
            rung = _CONFIDENCE_LADDER.index(entry.confidence)
            if helpful:
                new_rung = min(rung + 1, len(_CONFIDENCE_LADDER) - 1)
                updated = self.writer.update(
                    MemoryEntry.model_validate(
                        entry.model_dump(mode="python")
                        | {"confidence": _CONFIDENCE_LADDER[new_rung]}
                    )
                )
                action = "confidence_raised"
            elif rung == 0:
                files = write_review_queue(
                    [entry],
                    self.settings.data_dir,
                    reason=f"memory_feedback 不 helpful 且已为 low（note: {note or '无'}）",
                )
                self.writer.delete(memory_id)
                return {
                    "action": "queued_for_review",
                    "id": memory_id,
                    "queue_file": str(files[0]),
                    "note": note,
                }
            else:
                new_confidence = _CONFIDENCE_LADDER[rung - 1]
                try:
                    candidate = MemoryEntry.model_validate(
                        entry.model_dump(mode="python") | {"confidence": new_confidence}
                    )
                except ValueError:
                    files = write_review_queue(
                        [entry], self.settings.data_dir,
                        reason=(
                            f"负反馈要求降为 {new_confidence}，"
                            "但会违反条目 schema，需人工复核"
                        ),
                    )
                    return {
                        "action": "queued_for_review",
                        "id": memory_id,
                        "queue_file": str(files[0]),
                        "note": note,
                    }
                updated = self.writer.update(candidate)
                action = "confidence_lowered"
            return {
                "action": action,
                "id": memory_id,
                "confidence": updated.confidence,
                "note": note,
            }

    # ---- memory_update ----

    def update(self, memory_id: str, new_content: str) -> dict[str, Any]:
        """更新记忆正文：先脱敏，再过评价门（指令性/残留/长度规则同样适用）。"""
        validate_entry_id(memory_id)
        with interprocess_lock(self.writer.lock_path):
            entry = self.store.get(memory_id)
            redacted, _hits = redact(new_content)
            candidate = MemoryEntry.model_validate(
                entry.model_dump(mode="python") | {"content": redacted}
            )
            gate_result = gate_candidates([candidate])  # 不落盘，纯校验
            if gate_result.rejected:
                _, reason = gate_result.rejected[0]
                raise ValueError(f"评价门拒绝更新：{reason}")
            updated = self.writer.update(candidate, expected=entry)
        return {"action": "updated", "id": memory_id, "version": updated.version}

    # ---- memory_forget ----

    def forget(self, memory_id: str) -> dict[str, Any]:
        validate_entry_id(memory_id)
        self.writer.delete(memory_id)
        return {"action": "deleted", "id": memory_id}

    # ---- memory_review_list / memory_review_resolve（M5 人工复核交互） ----

    def review_list(self) -> dict[str, Any]:
        """列出复核队列的全部待办明细（不含 evolution/ 整理提案）。"""
        items = list_review_queue(self.settings.data_dir)
        return {"pending_review_count": len(items), "items": items}

    def review_resolve(
        self, queue_file: str, action: str, new_content: str | None = None
    ) -> dict[str, Any]:
        """人工裁决一条复核待办。

        action：
        - approve：确认按原样入库（人工裁决视为已核实，last_verified 刷新为今天）；
        - modify：以 new_content 替换正文后入库（过脱敏 + 评价门硬规则）；
        - discard：确认无价值，丢弃并删除队列文件。
        裁决成功后删除队列文件；raw_record 类待办（无法构造条目）不能直接入库。
        """
        if action not in {"approve", "modify", "discard"}:
            raise ValueError(f"非法 action {action!r}，只支持 approve / modify / discard")
        # 全局锁序固定为 memory_write → review_queue；feedback 在低置信度分支
        # 也按此顺序获取，避免两个路径互相等待。
        with interprocess_lock(self.writer.lock_path), review_queue_lock(
            self.settings.data_dir
        ):
            if action == "discard":
                delete_review_item(self.settings.data_dir, queue_file)
                return {"action": "discarded", "file": queue_file}
            payload = load_review_item(self.settings.data_dir, queue_file)
            if payload.get("entry") is None:
                raise ValueError(
                    "该待办是无法构造条目的原始记录（raw_record），不能直接入库；"
                    "请人工整理后用 memory_add 写入，或用 discard 丢弃"
                )
            entry = MemoryEntry.model_validate(payload["entry"])
            if action == "modify":
                if not new_content or not new_content.strip():
                    raise ValueError("action=modify 必须提供非空 new_content")
                redacted, _hits = redact(new_content)
                entry = MemoryEntry.model_validate(
                    entry.model_dump(mode="python") | {"content": redacted}
                )
                gate_result = gate_candidates([entry])
                if gate_result.rejected:
                    _, reason = gate_result.rejected[0]
                    raise ValueError(f"评价门拒绝入库：{reason}")
            entry = MemoryEntry.model_validate(
                entry.model_dump(mode="python") | {"last_verified": date.today()}
            )
            try:
                existing = self.store.get(entry.id)
            except KeyError:
                existing = None
            if existing is not None:
                stable_fields = (
                    "id", "content", "detail", "memory_type", "scope", "confidence",
                    "source", "evidence", "created_at", "supersedes",
                )
                if all(
                    getattr(existing, field) == getattr(entry, field)
                    for field in stable_fields
                ):
                    # Previous attempt may have committed the memory and failed only
                    # while deleting the queue file. Retrying completes that cleanup.
                    delete_review_item(self.settings.data_dir, queue_file)
                    return {
                        "action": "approved" if action == "approve" else "modified",
                        "id": entry.id,
                        "file": queue_file,
                        "already_applied": True,
                    }
                raise ValueError(
                    f"入库失败：id {entry.id!r} 已被不同内容占用，需先人工处理冲突"
                )
            try:
                self.writer.create(entry)
            except (MemoryStoreError, RuntimeError, ValueError) as e:
                raise ValueError(
                    f"入库失败：{e}（可先用 memory_forget / memory_update 处理冲突条目后重试）"
                ) from e
            delete_review_item(self.settings.data_dir, queue_file)
            return {
                "action": "approved" if action == "approve" else "modified",
                "id": entry.id,
                "file": queue_file,
            }

    # ---- memory_wm_read / memory_wm_write / memory_wm_clear（M7a 工作记忆） ----

    def _normalize_valid_scope(self, scope: str) -> str:
        """scope 归一化 + 校验（与 search 同口径）：非法当场报错，绝不静默。"""
        scope = normalize_scope(scope)
        if not is_valid_scope(scope):
            raise ValueError(
                "scope 非法：必须匹配 global | repo:<slug> | agent:<name>"
                f"（slug 为小写字母/数字/连字符），收到: {scope!r}"
            )
        return scope

    def wm_read(self, scope: str, current_turn: int | None = None) -> dict[str, Any]:
        """读取一个 scope 的工作记忆并渲染注入块。不存在时 exists=false（不是错误）。

        stale_wm：传了 current_turn 时按水位判断新鲜度（current_turn > turn_watermark
        说明工作记忆可能滞后，调用方可据此决定要不要先 wm_write 刷新）。
        """
        scope = self._normalize_valid_scope(scope)
        wm = self.working_store.read(scope)
        if wm is None:
            return {
                "status": "ok",
                "exists": False,
                "block": "",
                "working_memory": None,
                "stale_wm": False,
                "turn_watermark": 0,
            }
        block = render_working_memory_block(wm, self.settings.working_memory_budget_chars)
        stale = is_stale(wm, current_turn) if current_turn is not None else False
        return {
            "status": "ok",
            "exists": True,
            "block": block,
            "working_memory": wm.model_dump(mode="json"),
            "stale_wm": stale,
            "turn_watermark": wm.turn_watermark,
        }

    def wm_write(
        self,
        scope: str,
        goal: str | None = None,
        decisions: list[str] | None = None,
        variables: dict[str, str] | None = None,
        todos: list[dict | str] | None = None,
        notes: list[str] | None = None,
        turn_watermark: int | None = None,
    ) -> dict[str, Any]:
        """全量替换写入工作记忆（不是合并：未传的字段就是空）。

        所有文本字段逐个过脱敏（redact）；不过评价门、不做对账——工作记忆是
        操作层，TODO 天然是祈使句，过不了评价门。todos 兼容两种形式：
        [{"content": ..., "status": ...}, ...] 或纯字符串列表（按 pending）。
        turn_watermark 未传则保留旧值（新建为 0）。
        """
        scope = self._normalize_valid_scope(scope)
        redacted_fields = 0

        def _redact(text: str) -> str:
            nonlocal redacted_fields
            redacted, hits = redact(text)
            if hits:
                redacted_fields += 1
            return redacted

        old = self.working_store.read(scope)
        todo_items: list[TodoItem] = []
        for item in todos or []:
            if isinstance(item, str):
                todo_items.append(TodoItem(content=_redact(item)))
            elif isinstance(item, dict):
                data = {**item, "content": _redact(str(item.get("content", "")))}
                todo_items.append(TodoItem.model_validate(data))
            else:
                raise ValueError(
                    "todos 元素必须是字符串或 {content, status} 字典，"
                    f"收到: {type(item).__name__}"
                )
        wm = WorkingMemory(
            scope=scope,
            goal=_redact(goal) if goal else "",
            decisions=[_redact(d) for d in decisions or []],
            variables={_redact(k): _redact(v) for k, v in (variables or {}).items()},
            todos=todo_items,
            notes=[_redact(n) for n in notes or []],
            turn_watermark=(
                turn_watermark
                if turn_watermark is not None
                else (old.turn_watermark if old is not None else 0)
            ),
        )
        saved = self.working_store.write(
            wm, expected_version=old.version if old is not None else 0
        )
        return {"status": "ok", "version": saved.version, "redacted_fields": redacted_fields}

    def wm_clear(self, scope: str) -> dict[str, Any]:
        """清空一个 scope 的工作记忆。本就不存在时返回 already empty 而不报错
        （清空是幂等意图，不属于数据损坏）。"""
        scope = self._normalize_valid_scope(scope)
        try:
            self.working_store.delete(scope)
        except WorkingMemoryNotFoundError:
            return {"status": "ok", "note": "already empty"}
        return {"status": "ok"}

    # ---- memory_context（M7a 统一上下文组装） ----

    def context(
        self,
        scope: str,
        query: str | None = None,
        k: int = 5,
        current_turn: int | None = None,
        acknowledge_pending: bool = False,
    ) -> dict[str, Any]:
        """统一组装注入上下文，按框架顺序拼三个分节：

        1. 常驻画像块（当前 scope + global 的 profile 记忆）；
        2. 工作记忆块（当前 scope 的任务状态，预算 working_memory_budget_chars）；
        3. 召回块（query 给了才检索；复核门语义与 memory_search 一致——
           复核队列积压被拦截时整个 context 原样透出 blocked）。
        """
        scope = self._normalize_valid_scope(scope)
        pending_count, blocked = self._review_gate_block(acknowledge_pending)
        if blocked:
            return blocked
        profile = build_system_context(scope, store=self.store, settings=self.settings)
        wm = self.working_store.read(scope)
        wm_block = (
            render_working_memory_block(wm, self.settings.working_memory_budget_chars)
            if wm is not None
            else ""
        )
        stale = (
            is_stale(wm, current_turn)
            if wm is not None and current_turn is not None
            else False
        )
        recall_block = ""
        if query is not None:
            result = self.search(query, scope=scope, k=k, acknowledge_pending=acknowledge_pending)
            if result["status"] == "blocked":
                # 复核门语义不变：blocked 原样透出，不返回任何记忆内容
                return result
            recall_block = result["block"]
            pending_count = result["pending_review_count"]
        block = "\n\n".join(s for s in (profile, wm_block, recall_block) if s)
        return {
            "status": "ok",
            "block": block,
            "sections": {
                "profile": profile,
                "working_memory": wm_block,
                "recall": recall_block,
            },
            "stale_wm": stale,
            "pending_review_count": pending_count,
        }

    # ---- memory_transcript_read（M7b 短期记忆 transcript 读取） ----

    def transcript_read(
        self,
        log_path: str,
        adapter: str | None = None,
        since_turn: int | None = None,
    ) -> dict[str, Any]:
        """把 agent 会话日志解析成干净轮次序列（user/assistant/tool），纯读不写。

        adapter 缺省按文件名自动识别（detect_adapter），识别不了或显式传错
        名字都会 fail-closed 报错并列出可用适配器；日志不存在抛
        FileNotFoundError。since_turn 是新鲜度补偿的增量语义：给了就只返回
        turn_index > since_turn 的轮次（"水位之后"的新内容），配合
        memory_wm_read 的 turn_watermark 使用。
        """
        path = Path(log_path)
        ad = detect_adapter(path) if adapter is None else get_adapter(adapter)
        turns = ad.parse(path)
        if since_turn is not None:
            turns = [t for t in turns if t.turn_index > since_turn]
        return {
            "status": "ok",
            "adapter": ad.name,
            "turn_count": len(turns),
            "turns": [t.model_dump() for t in turns],
        }

    # ---- memory_session_end（M7b 会话结束编排） ----

    def session_end(
        self,
        scope: str,
        conversation_json: str | list | None = None,
        log_path: str | None = None,
        adapter: str | None = None,
        source: str = "mcp",
        session_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """会话结束收尾：归档原文 + 联合蒸馏 + 已完成 TODO 清理（顺序固定）。

        判定"会话是否结束"的三条规则里，前两条（人工明确结束、十分钟无活动）
        由宿主决定何时调用本工具；这里只强制执行第三条——未完成任务否决：
        工作记忆里有 pending todo 且 force=False 时返回 status=vetoed，
        归档/蒸馏/清理一律不执行；确认结束传 force=true 放行。

        材料二选一：conversation_json（[{role, content}, ...]，agent 中立推荐，
        兼容原生数组输入，与 memory_add 同样容错）优先；否则 log_path
        （走 detect_adapter/get_adapter 解析成轮次）。都没有报 ValueError。

        归档：原文原样追加到 data/raw/<source>/<session_id>.jsonl（每行一个
        JSON；conversation_json 路径写原始对话条目，log_path 路径写 Turn dump）
        ——D1 只追加不改写，同 session_id 调两次是追加不是覆盖。

        蒸馏：仅 user/assistant 轮次（adapter 路径的 tool 轮次剔除——
        蒸馏输入校验只认非空 content 的 role/content 对话，tool 轮次的机器
        输出不属对话正文）；工作记忆快照渲染成参考小节随 extra_context 传入。
        未配置 LLM 时跳过蒸馏与清理，返回 status=archived_only（归档已完成）。

        清理（仅当蒸馏真的执行了）：工作记忆里 status=done 的 todo 移除
        （结论已蒸馏沉淀），pending 保留，其余字段原样；有变动才写回。
        清理后整个工作记忆全空时在 wm_hint 里提示可 memory_wm_clear
        （不自动清，留给人/调用方决定）。
        """
        scope = self._normalize_valid_scope(scope)

        # 1. 未完成任务否决（三条结束判定规则里服务侧强制执行的一条）
        wm = self.working_store.read(scope)
        pending_todos = [t for t in wm.todos if t.status == "pending"] if wm else []
        if pending_todos and not force:
            return {
                "status": "vetoed",
                "pending_todos": [t.content for t in pending_todos],
                "message": (
                    f"工作记忆里还有 {len(pending_todos)} 条未完成任务，会话不视为结束；"
                    "确认要结束请以 force=true 重试（未完成任务会保留在工作记忆中）"
                ),
            }

        # 2. 取对话材料（conversation_json 优先）；归档内容同时在此定型
        archive_records: list[dict]
        if isinstance(conversation_json, list):
            # 与 memory_add 同款容错：原生数组代为序列化；空数组按未提供处理
            conversation_json = (
                json.dumps(conversation_json, ensure_ascii=False) if conversation_json else None
            )
        elif conversation_json is not None and not isinstance(conversation_json, str):
            raise ValueError(
                "conversation_json 格式错误：请传 [{role, content}, ...] 的 JSON "
                f"字符串（或等价的数组），收到的是 {type(conversation_json).__name__}"
            )
        if conversation_json:
            # 先解析校验（非法对话不归档——不把垃圾写进 data/raw）
            conversation = parse_conversation_json(conversation_json)
            archive_records = conversation
            distill_input = conversation_json
        elif log_path:
            path = Path(log_path)
            ad = detect_adapter(path) if adapter is None else get_adapter(adapter)
            turns = ad.parse(path)
            archive_records = [t.model_dump(mode="json") for t in turns]
            # 蒸馏只用 user/assistant 正文轮次：tool 轮次剔除（机器输出非对话
            # 正文）；空内容的轮次（如只发了工具调用的 assistant 轮）同样剔除，
            # 否则会触发 parse_conversation_json 的非空 content 校验
            conversation = [
                {"role": t.role, "content": t.content}
                for t in turns
                if t.role in ("user", "assistant") and t.content.strip()
            ]
            distill_input = json.dumps(conversation, ensure_ascii=False)
        else:
            raise ValueError(
                "session_end 需要对话材料：conversation_json（[{role, content}, ...]"
                " 的 JSON 字符串或数组，agent 中立推荐）或 log_path（agent 会话日志"
                " 路径，走日志适配器解析）二选一"
            )
        session_id = session_id or f"session-{datetime.now():%Y%m%dT%H%M%S}"

        # 3. 归档：data/raw/<source>/<session_id>.jsonl，只追加不改写（D1）
        archive_path, evidence_line_offset = self._archive_raw(
            archive_records, source, session_id
        )
        evidence_line_map = None
        if log_path:
            evidence_line_map = [
                evidence_line_offset + index + 1
                for index, record in enumerate(archive_records)
                if record.get("role") in {"user", "assistant"}
                and str(record.get("content", "")).strip()
            ]

        # 4. 蒸馏：无 LLM 时只归档，工作记忆保持原样
        if self.llm is None:
            return {
                "status": "archived_only",
                "session_id": session_id,
                "archive_path": str(archive_path),
                "warning": (
                    "归档已完成，但未配置 LLM（AGENT_MEMORY_LLM_API_KEY），无法蒸馏，"
                    "工作记忆保持原样（已完成的 TODO 未清理）"
                ),
            }

        # 工作记忆快照作为蒸馏的参考上下文（宽松预算：蒸馏材料不进注入层，
        # 不受 working_memory_budget_chars 限制）
        extra_context = (
            render_working_memory_block(wm, _SESSION_END_WM_SNAPSHOT_BUDGET_CHARS)
            if wm is not None
            else None
        ) or None
        report = self._add_conversation(
            distill_input, scope, source, session_id, extra_context=extra_context,
            archive=False,  # 本会话已在第 3 步归档，避免同 session_id 重复追加
            evidence_line_offset=evidence_line_offset,
            evidence_line_map=evidence_line_map,
        )
        if report.status != "ok":
            # 蒸馏未执行（LLM 临时故障）：归档已完成，工作记忆保持原样不清理，
            # LLM 恢复后重跑本调用即可（同 session_id 归档是追加，不覆盖）
            return {
                "status": report.status,
                "session_id": session_id,
                "archive_path": str(archive_path),
                "warning": report.warning,
            }

        # 5. 清理已完成 TODO（结论已蒸馏沉淀；pending 保留，其余字段原样）
        todos_cleared = 0
        todos_kept = 0
        wm_hint = None
        # 只有至少一条候选实际入库或进入人工复核，才能认为 done todo 的结论
        # 已有落点；全被评价门拒绝时保留工作记忆。
        has_durable_result = sum(report.reconcile.values()) > 0 or bool(
            report.pending_review
        )
        if wm is not None and has_durable_result:
            current_wm = self.working_store.read(scope)
            if current_wm is not None:
                original_done = {t.content for t in wm.todos if t.status == "done"}
                kept = [
                    t
                    for t in current_wm.todos
                    if not (t.status == "done" and t.content in original_done)
                ]
                todos_cleared = len(current_wm.todos) - len(kept)
                todos_kept = len(kept)
                if todos_cleared:
                    wm = self.working_store.write(
                        current_wm.model_copy(update={"todos": kept}),
                        expected_version=current_wm.version,
                    )
                else:
                    wm = current_wm
            if (
                not wm.goal and not wm.decisions and not wm.variables
                and not wm.todos and not wm.notes
            ):
                wm_hint = "工作记忆已全空，可用 memory_wm_clear 清空（本次不自动清理）"

        result: dict[str, Any] = {
            "status": "ok",
            "session_id": session_id,
            "archive_path": str(archive_path),
            "distill": asdict(report),
            "todos_cleared": todos_cleared,
            "todos_kept": todos_kept,
            "pending_review": report.pending_review,
        }
        if wm_hint:
            result["wm_hint"] = wm_hint
        return result


def build_server(service: MemoryService):
    """把 MemoryService 注册成 MCP server 的十五个 tool。"""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "agent-memory",
        description="本地记忆服务：长期记忆（检索/写入/反馈/复核）+ 工作记忆 + 上下文组装",
    )

    @server.tool(
        name="memory_search",
        description=(
            "检索历史记忆，返回注入用 XML 块与结构化命中列表。scope 自动归一化"
            "（下划线等旧写法折叠为连字符），非法 scope 当场报错而非静默返回空。"
            "复核队列有积压时"
            "按 review_gate 配置处置：返回 status=blocked 表示被复核门拦截，"
            "需先向用户确认（用户同意后以 acknowledge_pending=true 重试，或先"
            "用 memory_review_list / memory_review_resolve 处理待办）"
        ),
    )
    def memory_search(
        query: str, scope: str = "global", k: int = 5, acknowledge_pending: bool = False
    ) -> dict:
        return service.search(query, scope=scope, k=k, acknowledge_pending=acknowledge_pending)

    @server.tool(
        name="memory_add",
        description=(
            "写入记忆：对话走蒸馏管线，宿主蒸馏候选走 distilled_json，单条 content "
            "走脱敏+对账。conversation_json 推荐传 [{role, content}, ...] 的 JSON 字符串"
            "（直接传数组也可以，服务端会自动序列化；其他类型会报错并提示格式）。"
            "distilled_json 是宿主蒸馏模式：先用 memory_distill_prompt 拿蒸馏协议自行蒸馏，"
            "再把 {\"memories\": [...]} 的 JSON 字符串提交进来（候选照常过服务端"
            "校验/脱敏/评价门/对账，服务端无 LLM 也能用）。"
            "scope 应显式选择：跨项目通用知识用 global，项目相关用 repo:<项目名>，"
            "agent 自身相关用 agent:<名字>；缺省回落 global 并附提醒。"
            "失败语义：评价门拒绝是确定性规则拦截（报错含命中片段，原样重试无效；"
            "确认误伤可 force_review=true 转人工复核）；LLM 失败是临时性故障"
            "（对话原文已归档 data/raw，返回 status=archived_only，稍后重试即可）。"
            + _SUBAGENT_WRITE_GUARD
        ),
    )
    def memory_add(
        content: str | None = None,
        conversation_json: str | list | None = None,
        distilled_json: str | None = None,
        scope: str | None = None,
        source: str = "mcp",
        session_id: str | None = None,
        entry_id: str | None = None,
        memory_type: str = "semantic",
        confidence: str = "high",
        force_review: bool = False,
    ) -> dict:
        return service.add(
            content=content,
            conversation_json=conversation_json,
            distilled_json=distilled_json,
            scope=scope,
            source=source,
            session_id=session_id,
            entry_id=entry_id,
            memory_type=memory_type,
            confidence=confidence,
            force_review=force_review,
        )

    @server.tool(
        name="memory_distill_prompt",
        description=(
            "返回宿主蒸馏协议：蒸馏用的 system prompt、输出 JSON schema、对话渲染格式"
            "与使用说明。无服务端 LLM（订阅制 agent，无 API key）时，用它在自己的"
            "上下文里完成对话蒸馏，再以 memory_add(distilled_json=...) 提交候选。"
        ),
    )
    def memory_distill_prompt() -> dict:
        return service.distill_protocol()

    @server.tool(
        name="memory_consistency_check",
        description=(
            "只读检查 Markdown 记忆事实层与 SQLite 派生索引是否一致；"
            "返回仅存在于任一侧的条目 id，不修改任何数据。"
        ),
    )
    def memory_consistency_check() -> dict:
        return service.consistency()

    @server.tool(
        name="memory_feedback",
        description=(
            "反馈记忆是否有用，调整置信度；降到 low 以下进人工复核队列。"
            + _SUBAGENT_WRITE_GUARD
        ),
    )
    def memory_feedback(memory_id: str, helpful: bool, note: str | None = None) -> dict:
        return service.feedback(memory_id, helpful, note)

    @server.tool(
        name="memory_update",
        description="更新一条记忆的正文（过脱敏与评价门）。" + _SUBAGENT_WRITE_GUARD,
    )
    def memory_update(memory_id: str, new_content: str) -> dict:
        return service.update(memory_id, new_content)

    @server.tool(
        name="memory_forget",
        description="删除一条记忆（记忆层与索引同步删除）。" + _SUBAGENT_WRITE_GUARD,
    )
    def memory_forget(memory_id: str) -> dict:
        return service.forget(memory_id)

    @server.tool(
        name="memory_review_list",
        description="列出人工复核队列的全部待办（内容、排队原因、队列文件名）",
    )
    def memory_review_list() -> dict:
        return service.review_list()

    @server.tool(
        name="memory_review_resolve",
        description=(
            "裁决一条复核待办：approve 确认入库 / modify 以 new_content 替换正文后入库 "
            "/ discard 丢弃。queue_file 取 memory_review_list 返回里的 file 字段。"
            + _SUBAGENT_WRITE_GUARD
        ),
    )
    def memory_review_resolve(
        queue_file: str, action: str, new_content: str | None = None
    ) -> dict:
        return service.review_resolve(queue_file, action, new_content)

    @server.tool(
        name="memory_wm_read",
        description=(
            "读取一个 scope 的工作记忆（当前任务状态：目标/待办/决策/变量/备注），"
            "返回渲染好的注入块与结构化字段。scope 自动归一化，非法当场报错。"
            "传 current_turn 时返回 stale_wm 表示工作记忆是否可能滞后"
            "（当前轮次超过已更新到的轮次水位），滞后可考虑 wm_write 刷新"
        ),
    )
    def memory_wm_read(scope: str, current_turn: int | None = None) -> dict:
        return service.wm_read(scope, current_turn=current_turn)

    @server.tool(
        name="memory_wm_write",
        description=(
            "写入工作记忆（当前任务状态）。注意是全量替换而非合并：未传的字段"
            "会被置空，只想改一个字段也要把其余字段原样带上。所有文本过脱敏；"
            "不过评价门——待办事项天然是祈使句，属于正常内容。todos 可传"
            " [{content, status}, ...]（status 为 pending/done）或纯字符串列表"
            "（按 pending）。turn_watermark 传当前对话轮次；未传保留旧值。"
            + _SUBAGENT_WRITE_GUARD
        ),
    )
    def memory_wm_write(
        scope: str,
        goal: str | None = None,
        decisions: list[str] | None = None,
        variables: dict[str, str] | None = None,
        todos: list[dict | str] | None = None,
        notes: list[str] | None = None,
        turn_watermark: int | None = None,
    ) -> dict:
        return service.wm_write(
            scope,
            goal=goal,
            decisions=decisions,
            variables=variables,
            todos=todos,
            notes=notes,
            turn_watermark=turn_watermark,
        )

    @server.tool(
        name="memory_wm_clear",
        description=(
            "清空一个 scope 的工作记忆；本就不存在时返回 already empty，不算错误。"
            + _SUBAGENT_WRITE_GUARD
        ),
    )
    def memory_wm_clear(scope: str) -> dict:
        return service.wm_clear(scope)

    @server.tool(
        name="memory_context",
        description=(
            "统一组装注入上下文：常驻画像块（长期用户画像）+ 工作记忆块"
            "（当前任务状态）+ 召回块（传 query 才检索历史记忆），按此顺序拼接。"
            "复核队列有积压时按 review_gate 配置处置：返回 status=blocked 表示"
            "被复核门拦截，需先向用户确认（用户同意后以 acknowledge_pending=true"
            "重试，或先用 memory_review_list / memory_review_resolve 处理待办）"
        ),
    )
    def memory_context(
        scope: str,
        query: str | None = None,
        k: int = 5,
        current_turn: int | None = None,
        acknowledge_pending: bool = False,
    ) -> dict:
        return service.context(
            scope,
            query=query,
            k=k,
            current_turn=current_turn,
            acknowledge_pending=acknowledge_pending,
        )

    @server.tool(
        name="memory_transcript_read",
        description=(
            "读取 agent 会话日志（如 kimi-code 的 wire.jsonl），解析成干净的"
            "轮次序列（user/assistant/tool，含轮次编号与时间戳）。纯读不写。"
            "配合工作记忆水位做新鲜度补偿：传 since_turn=<memory_wm_read 返回的"
            " turn_watermark> 只返回水位之后的新轮次，据此判断要不要 wm_write"
            " 刷新工作记忆。adapter 缺省按日志文件名自动识别，识别不了需显式"
            "指定（可用列表见报错信息）；日志不存在会报错"
        ),
    )
    def memory_transcript_read(
        log_path: str, adapter: str | None = None, since_turn: int | None = None
    ) -> dict:
        return service.transcript_read(log_path, adapter=adapter, since_turn=since_turn)

    @server.tool(
        name="memory_session_end",
        description=(
            "会话结束收尾编排：归档原文（data/raw，只追加不改写）+ 联合蒸馏"
            "（对话提炼长期记忆，工作记忆快照作参考上下文，冲突会更新旧条目）"
            " + 清理工作记忆里已完成的待办。工作记忆有未完成任务时会 veto"
            "（status=vetoed，归档/蒸馏/清理都不执行），确认结束请以 force=true"
            " 重试。对话材料二选一：conversation_json（[{role, content}, ...]"
            " 的 JSON 字符串或数组，agent 中立推荐，优先使用）或 log_path"
            "（agent 会话日志路径，走日志适配器解析，adapter 可缺省按文件名"
            " 自动识别）。未配置 LLM 或 LLM 调用失败时只归档不蒸馏"
            "（status=archived_only，LLM 恢复后重跑即可）。"
            + _SUBAGENT_WRITE_GUARD
        ),
    )
    def memory_session_end(
        scope: str,
        conversation_json: str | list | None = None,
        log_path: str | None = None,
        adapter: str | None = None,
        source: str = "mcp",
        session_id: str | None = None,
        force: bool = False,
    ) -> dict:
        return service.session_end(
            scope,
            conversation_json=conversation_json,
            log_path=log_path,
            adapter=adapter,
            source=source,
            session_id=session_id,
            force=force,
        )

    return server


def main() -> None:
    """stdio server 入口：uv run python -m agent_memory.server.mcp_server"""
    settings = get_settings()
    store = MarkdownStore(settings.data_dir)
    index = IndexDB(settings.data_dir / "index.db")
    embedder = get_embedder(settings)
    # LLM 缺失不阻止启动：search/update/forget/feedback 仍可用，
    # 只有 conversation 蒸馏路径在调用时 fail-closed
    llm: LLMClient | None
    try:
        llm = OpenAILLMClient.from_settings(settings)
    except LLMError as e:
        print(f"[agent-memory] 警告：{e}；memory_add 的对话蒸馏模式将不可用", file=sys.stderr)
        llm = None
    server = build_server(MemoryService(settings, store, index, embedder, llm))
    try:
        server.run("stdio")
    finally:
        index.close()


if __name__ == "__main__":
    main()
