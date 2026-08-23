"""MCP Server（M2/M5/M7a）：把记忆内核暴露为十一个 MCP tool，stdio 传输。

长期记忆 tool（七个）：
- memory_search：混合检索 + render_recall_block 渲染，scope 过滤在检索层强制
  （只查调用方给的 scope + global，global 由 HybridSearcher 自动并入）。
  M5 复核门：复核队列有积压时按 settings.review_gate 处置——ask 档拦截并等
  用户确认（acknowledge_pending=true 放行），strict 档一律拒读，off 不拦；
- memory_add：conversation_json 走 蒸馏→评价门→对账 全管线；单条 content 走
  脱敏→评价门→对账。返回各阶段报告 + pending_review 待复核明细（M5）；
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

启动：uv run python -m agent_memory.server.mcp_server
组件构建在 main() 里完成；MemoryService 是纯 Python 类，测试直接注入
fake embedder / fake LLM / fake working store 调用，不走 MCP 传输。
"""

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from agent_memory.config import Settings, get_settings
from agent_memory.llm import LLMClient, LLMError, OpenAILLMClient
from agent_memory.long_term.ingest.distill import distill_memories, parse_conversation_json
from agent_memory.long_term.ingest.gate import gate_candidates, write_review_queue
from agent_memory.long_term.ingest.reconcile import reconcile
from agent_memory.long_term.ingest.redact import redact
from agent_memory.long_term.ingest.review_queue import (
    delete_review_item,
    list_review_queue,
    load_review_item,
)
from agent_memory.long_term.retrieve.embedder import get_embedder
from agent_memory.long_term.retrieve.hybrid import HybridSearcher
from agent_memory.long_term.retrieve.inject import render_recall_block
from agent_memory.long_term.retrieve.resident import build_system_context
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore, MemoryStoreError
from agent_memory.models import MemoryEntry, is_valid_scope, normalize_scope
from agent_memory.working.models import TodoItem, WorkingMemory
from agent_memory.working.render import is_stale, render_working_memory_block
from agent_memory.working.store import WorkingMemoryNotFoundError, WorkingMemoryStore

# confidence 的升降阶梯：feedback 沿它走一步
_CONFIDENCE_LADDER = ["low", "medium", "high"]


@dataclass
class AddReport:
    """memory_add 的分阶段报告。"""

    mode: str  # "distill"（对话蒸馏）| "direct"（单条内容）
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


_SCOPE_REMINDER = (
    "本次写入使用了默认 scope=global（全局共享，对所有项目可见）。"
    "若该记忆只与某个项目或某个 agent 相关，应显式指定 scope 为 "
    "repo:<项目名> 或 agent:<名字>；写错了可 memory_forget 后按正确 scope 重存。"
)


def _pending_from_entry(entry: MemoryEntry, reason: str) -> dict:
    return {"id": entry.id, "content_preview": entry.content[:80], "reason": reason}


def _pending_from_raw(raw: object, reason: str) -> dict:
    rid = raw.get("id") if isinstance(raw, dict) else None
    content = raw.get("content") if isinstance(raw, dict) else str(raw)
    return {"id": rid, "content_preview": str(content)[:80], "reason": reason}


class MemoryService:
    """十一个 tool 的业务实现。与 MCP 传输解耦，测试直接实例化调用。"""

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
        self.llm = llm
        # 工作记忆存储缺省按 settings.data_dir 自建；测试可注入以隔离
        self.working_store = working_store or WorkingMemoryStore(settings.data_dir)
        self.searcher = HybridSearcher(store, index, embedder, settings)

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
        pending_count = len(list_review_queue(self.settings.data_dir))
        gate = self.settings.review_gate
        if pending_count and gate == "strict":
            return {
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
            return {
                "status": "blocked",
                "gate": gate,
                "pending_review_count": pending_count,
                "message": (
                    f"记忆库有 {pending_count} 条人工复核尚未确认。请先向用户确认："
                    "现在逐条处理（memory_review_list 查看 + memory_review_resolve 裁决），"
                    "还是本次照常读取（用户明确同意后以 acknowledge_pending=true 重试本调用）。"
                ),
            }
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
        scope: str | None = None,
        source: str = "mcp",
        session_id: str | None = None,
        entry_id: str | None = None,
        memory_type: str = "semantic",
        confidence: str = "high",
    ) -> dict[str, Any]:
        """写入记忆。conversation_json 与 content 二选一（都给了以对话为准）。

        conversation_json 推荐传 [{role, content}, ...] 的 JSON 字符串；直接传
        list 也可以（服务端自动序列化）。其他类型报 ValueError 并提示正确格式。

        scope 应显式选择（M6 作用域纪律）：global 放跨项目通用知识，repo:<项目名>
        放项目相关，agent:<名字> 放 agent 自身相关。缺省回落 global 并附提醒。
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
            report = self._add_conversation(conversation_json, scope, source, session_id)
        else:
            if content is None:
                raise ValueError("memory_add 需要 content 或 conversation_json 之一")
            report = self._add_direct(content, scope, source, entry_id, memory_type, confidence)
        report.scope_reminder = scope_reminder
        return asdict(report)

    def _add_conversation(
        self, conversation_json: str, scope: str, source: str, session_id: str | None
    ) -> AddReport:
        if self.llm is None:
            raise LLMError(
                "未配置 LLM（AGENT_MEMORY_LLM_API_KEY），无法蒸馏对话；"
                "可改用 content 参数走单条手动写入"
            )
        conversation = parse_conversation_json(conversation_json)
        session_id = session_id or f"session-{datetime.now():%Y%m%dT%H%M%S}"
        distill_result = distill_memories(
            conversation, scope, source, session_id, self.llm,
            data_dir=self.settings.data_dir,
        )
        gate_result = gate_candidates(distill_result.entries, self.settings.data_dir)
        report = reconcile(
            gate_result.passed, self.store, self.index, self.llm,
            embedder=self.embedder, settings=self.settings,
        )
        return AddReport(
            mode="distill",
            distilled=len(distill_result.entries),
            normalized_ids=distill_result.normalized_ids,
            queued_invalid=len(distill_result.invalid_records),
            dropped_redacted=distill_result.dropped_redacted,
            gate_rejected=[
                {"id": e.id, "reason": reason} for e, reason in gate_result.rejected
            ],
            gate_queued=[e.id for e in gate_result.queued],
            reconcile=report.counts(),
            pending_review=(
                [
                    _pending_from_raw(raw, f"蒸馏产出非法：{item_reason}")
                    for raw, item_reason in distill_result.invalid_records
                ]
                + [
                    _pending_from_entry(e, "confidence=low，规则门分流")
                    for e in gate_result.queued
                ]
                + [_pending_from_entry(e, reason) for e, reason in report.queued]
            ),
        )

    def _add_direct(
        self,
        content: str,
        scope: str,
        source: str,
        entry_id: str | None,
        memory_type: str,
        confidence: str,
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
            _, reason = gate_result.rejected[0]
            raise ValueError(f"评价门拒绝入库：{reason}")
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
            ),
        )

    # ---- memory_feedback ----

    def feedback(self, memory_id: str, helpful: bool, note: str | None = None) -> dict[str, Any]:
        """反馈调整 confidence：helpful 升一档，不 helpful 降一档；
        已是 low 再降就移出正式库、写入复核队列。"""
        entry = self.store.get(memory_id)  # 找不到抛 KeyError，fail-closed
        rung = _CONFIDENCE_LADDER.index(entry.confidence)
        if helpful:
            new_rung = min(rung + 1, len(_CONFIDENCE_LADDER) - 1)
            updated = self.store.update(
                entry.model_copy(update={"confidence": _CONFIDENCE_LADDER[new_rung]})
            )
            self.index.upsert(updated, self.embedder.embed_texts([updated.index_text])[0])
            return {
                "action": "confidence_raised",
                "id": memory_id,
                "confidence": updated.confidence,
                "note": note,
            }
        if rung == 0:
            # low 再降：移出正式库，进人工复核队列
            files = write_review_queue(
                [entry],
                self.settings.data_dir,
                reason=f"memory_feedback 不 helpful 且已为 low（note: {note or '无'}）",
            )
            self.store.delete(memory_id)
            self.index.delete(memory_id)
            return {
                "action": "queued_for_review",
                "id": memory_id,
                "queue_file": str(files[0]),
                "note": note,
            }
        updated = self.store.update(
            entry.model_copy(update={"confidence": _CONFIDENCE_LADDER[rung - 1]})
        )
        self.index.upsert(updated, self.embedder.embed_texts([updated.index_text])[0])
        return {
            "action": "confidence_lowered",
            "id": memory_id,
            "confidence": updated.confidence,
            "note": note,
        }

    # ---- memory_update ----

    def update(self, memory_id: str, new_content: str) -> dict[str, Any]:
        """更新记忆正文：先脱敏，再过评价门（指令性/残留/长度规则同样适用）。"""
        entry = self.store.get(memory_id)
        redacted, _hits = redact(new_content)
        candidate = entry.model_copy(update={"content": redacted})
        gate_result = gate_candidates([candidate])  # 不落盘，纯校验
        if gate_result.rejected:
            _, reason = gate_result.rejected[0]
            raise ValueError(f"评价门拒绝更新：{reason}")
        updated = self.store.update(candidate)
        self.index.upsert(updated, self.embedder.embed_texts([updated.index_text])[0])
        return {"action": "updated", "id": memory_id, "version": updated.version}

    # ---- memory_forget ----

    def forget(self, memory_id: str) -> dict[str, Any]:
        self.store.get(memory_id)  # 先确认存在，找不到 fail-closed
        self.store.delete(memory_id)
        self.index.delete(memory_id)
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
            entry = entry.model_copy(update={"content": redacted})
            gate_result = gate_candidates([entry])  # 不落盘，纯校验硬规则
            if gate_result.rejected:
                _, reason = gate_result.rejected[0]
                raise ValueError(f"评价门拒绝入库：{reason}")
        # 人工裁决即"已核实"：刷新核实时间（created_at 保留原值）
        entry = entry.model_copy(update={"last_verified": date.today()})
        try:
            self.store.create(entry)
        except MemoryStoreError as e:
            raise ValueError(
                f"入库失败：{e}（可先用 memory_forget / memory_update 处理冲突条目后重试）"
            ) from e
        self.index.upsert(entry, self.embedder.embed_texts([entry.index_text])[0])
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
        saved = self.working_store.write(wm)
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
        pending_count = len(list_review_queue(self.settings.data_dir))
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


def build_server(service: MemoryService):
    """把 MemoryService 注册成 MCP server 的十一个 tool。"""
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
            "写入记忆：对话走蒸馏管线，单条 content 走脱敏+对账。"
            "conversation_json 推荐传 [{role, content}, ...] 的 JSON 字符串"
            "（直接传数组也可以，服务端会自动序列化；其他类型会报错并提示格式）。"
            "scope 应显式选择：跨项目通用知识用 global，项目相关用 repo:<项目名>，"
            "agent 自身相关用 agent:<名字>；缺省回落 global 并附提醒"
        ),
    )
    def memory_add(
        content: str | None = None,
        conversation_json: str | list | None = None,
        scope: str | None = None,
        source: str = "mcp",
        session_id: str | None = None,
        entry_id: str | None = None,
        memory_type: str = "semantic",
        confidence: str = "high",
    ) -> dict:
        return service.add(
            content=content,
            conversation_json=conversation_json,
            scope=scope,
            source=source,
            session_id=session_id,
            entry_id=entry_id,
            memory_type=memory_type,
            confidence=confidence,
        )

    @server.tool(
        name="memory_feedback",
        description="反馈记忆是否有用，调整置信度；降到 low 以下进人工复核队列",
    )
    def memory_feedback(memory_id: str, helpful: bool, note: str | None = None) -> dict:
        return service.feedback(memory_id, helpful, note)

    @server.tool(name="memory_update", description="更新一条记忆的正文（过脱敏与评价门）")
    def memory_update(memory_id: str, new_content: str) -> dict:
        return service.update(memory_id, new_content)

    @server.tool(name="memory_forget", description="删除一条记忆（记忆层与索引同步删除）")
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
            "/ discard 丢弃。queue_file 取 memory_review_list 返回里的 file 字段"
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
            "（按 pending）。turn_watermark 传当前对话轮次；未传保留旧值"
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
        description="清空一个 scope 的工作记忆；本就不存在时返回 already empty，不算错误",
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
