"""LangGraph BaseStore 适配（M3）：把 agent-memory 内核包装成 LangGraph 的长期记忆 store。

namespace 约定：("memories", <scope>)，第二段直接就是记忆系统的 scope
（"global" / "repo:<slug>" / "agent:<name>"），例如 ("memories", "repo:myproj")。
search 的 namespace_prefix 允许只给 ("memories",)，表示跨全部 scope 检索。

写路径（红线 D2 的适配）：
- BaseStore 的 put 是同步低层接口，调用方必须自己给提炼好的原子内容——
  本适配器不走 LLM 蒸馏，但 脱敏（redact）与评价门（gate）的规则部分照过：
  指令性内容 / 脱敏残留 / 长度不足的 put 直接抛 ValueError（fail-closed）；
  confidence=low 的 put 不进正式库，写入 review_queue 人工复核。
- put 是 upsert 语义：key 已存在走 MarkdownStore.update（version 自动 +1），
  否则 create；删除按 LangGraph 约定是 put(namespace, key, None)。

抽象方法只有 batch / abatch（langgraph 1.x：get/search/put/delete 都是 batch 的
语法糖），所以核心是实现 batch；abatch 用 asyncio.to_thread 包一层同步实现——
SQLite 连接是 check_same_thread=False 且本适配器调用串行，这样包是安全的。
"""

import asyncio
from datetime import date, datetime, time
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from agent_memory.config import Settings, get_settings
from agent_memory.long_term.ingest.gate import gate_candidates
from agent_memory.long_term.ingest.redact import redact
from agent_memory.long_term.retrieve.embedder import get_embedder
from agent_memory.long_term.retrieve.hybrid import HybridSearcher
from agent_memory.long_term.store.coordinator import MemoryWriter
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore, MemoryNotFoundError
from agent_memory.models import MemoryEntry, is_valid_scope, normalize_scope

NAMESPACE_ROOT = "memories"

# put 时 value 里可选字段的默认值
_DEFAULT_MEMORY_TYPE = "semantic"
_DEFAULT_CONFIDENCE = "medium"


def namespace_to_scope(namespace: tuple[str, ...]) -> str:
    """("memories", <scope>) -> scope。不符合约定直接抛 ValueError（fail-closed）。"""
    if len(namespace) != 2 or namespace[0] != NAMESPACE_ROOT:
        raise ValueError(
            f"namespace 必须是 ({NAMESPACE_ROOT!r}, <scope>) 形式，"
            f"例如 ('memories', 'repo:myproj')，收到: {namespace!r}"
        )
    scope = normalize_scope(namespace[1])
    if not is_valid_scope(scope):
        raise ValueError(f"namespace 中的 scope 非法: {namespace[1]!r}")
    return scope


def scope_to_namespace(scope: str) -> tuple[str, str]:
    """scope -> ("memories", scope)。"""
    return (NAMESPACE_ROOT, scope)


def _entry_value(entry: MemoryEntry) -> dict[str, Any]:
    """MemoryEntry -> LangGraph Item 的 value dict（可 JSON 序列化）。"""
    return {
        "content": entry.content,
        "detail": entry.detail,
        "memory_type": entry.memory_type,
        "confidence": entry.confidence,
        "scope": entry.scope,
        "source": entry.source,
        "last_verified": entry.last_verified.isoformat(),
        "version": entry.version,
    }


def _to_item(entry: MemoryEntry) -> Item:
    return Item(
        value=_entry_value(entry),
        key=entry.id,
        namespace=scope_to_namespace(entry.scope),
        created_at=datetime.combine(entry.created_at, time.min),
        updated_at=datetime.combine(entry.last_verified, time.min),
    )


class AgentMemoryStore(BaseStore):
    """LangGraph BaseStore 的 agent-memory 实现。

    构造参数缺省时按 AGENT_MEMORY_* 环境变量自建（settings / 默认 embedder 单例）；
    测试注入临时目录的 store/index 与 fake embedder 即可。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        store: MarkdownStore | None = None,
        index: IndexDB | None = None,
        embedder=None,
    ):
        self.settings = settings or get_settings()
        self.store = store or MarkdownStore(self.settings.data_dir)
        self.index = index or IndexDB(self.settings.data_dir / "index.db")
        self.embedder = embedder or get_embedder(self.settings)
        self.searcher = HybridSearcher(self.store, self.index, self.embedder, self.settings)
        self.writer = MemoryWriter(self.store, self.index, self.embedder)

    # ---- batch：四种 Op 的分发 ----

    def batch(self, ops: list[Op]) -> list[Result]:
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._handle_get(op))
            elif isinstance(op, SearchOp):
                results.append(self._handle_search(op))
            elif isinstance(op, PutOp):
                results.append(self._handle_put(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._handle_list_namespaces(op))
            else:
                raise NotImplementedError(f"不支持的 Op 类型: {type(op).__name__}")
        return results

    async def abatch(self, ops: list[Op]) -> list[Result]:
        # 同步实现包一层线程池：SQLite/embedder 都是同步 API，串行调用下安全
        return await asyncio.to_thread(self.batch, list(ops))

    # ---- 各 Op 的处理 ----

    def _handle_get(self, op: GetOp) -> Item | None:
        scope = namespace_to_scope(op.namespace)
        try:
            entry = self.store.get(op.key)
        except MemoryNotFoundError:
            return None
        return _to_item(entry) if entry.scope == scope else None

    def _handle_put(self, op: PutOp) -> None:
        scope = namespace_to_scope(op.namespace)
        if op.value is None:
            # LangGraph 约定：value=None 即 delete
            existing = self.store.get(op.key)
            if existing.scope != scope:
                raise KeyError(f"namespace {op.namespace!r} 下不存在 key {op.key!r}")
            self.writer.delete(op.key)
            return None

        content = str(op.value.get("content", ""))
        redacted, _hits = redact(content)
        detail = op.value.get("detail") or None
        if detail is not None:
            detail = redact(str(detail))[0]
        today = date.today()
        entry = MemoryEntry(
            id=op.key,
            content=redacted,
            detail=detail,
            memory_type=op.value.get("memory_type", _DEFAULT_MEMORY_TYPE),
            scope=scope,
            confidence=op.value.get("confidence", _DEFAULT_CONFIDENCE),
            source=op.value.get("source", "langgraph"),
            created_at=today,
            last_verified=today,
        )
        # 过评价门的规则部分（不做 LLM 蒸馏：put 的调用方自己给提炼好的内容）
        gate_result = gate_candidates([entry], self.settings.data_dir)
        if gate_result.rejected:
            _, reason = gate_result.rejected[0]
            raise ValueError(f"评价门拒绝 put：{reason}")
        if gate_result.queued:
            # confidence=low：已写入 review_queue，不进正式库（put 视同落到了复核队列）
            return None

        try:
            existing = self.store.get(op.key)
        except MemoryNotFoundError:
            existing = None
        if existing is None:
            self.writer.create(entry)
        else:
            # upsert 语义：已有条目走 update（version 自动 +1、刷新 last_verified）；
            # 新 value 未给的字段继承旧条目，避免 put 把元数据抹掉
            if existing.scope != scope:
                raise ValueError(
                    f"key {op.key!r} 已存在于 scope={existing.scope!r}，"
                    "不能跨 scope/namespace 覆盖"
                )
            entry = self.writer.update(
                MemoryEntry.model_validate(
                    entry.model_dump(mode="python")
                    | {
                        "created_at": existing.created_at,
                        "evidence": existing.evidence,
                        "detail": entry.detail if "detail" in op.value else existing.detail,
                    }
                )
            )
        return None

    def _handle_search(self, op: SearchOp) -> list[SearchItem]:
        # namespace_prefix 为 ("memories",) 时跨全部 scope（scopes=None 不过滤）；
        # 为 ("memories", <scope>) 时只查该 scope（global 由 HybridSearcher 自动并入）
        if len(op.namespace_prefix) == 1 and op.namespace_prefix[0] == NAMESPACE_ROOT:
            scopes = None
        else:
            scopes = [namespace_to_scope(tuple(op.namespace_prefix))]

        # filter 支持 memory_type / confidence 两个字段的精确匹配，其余键报错（fail-closed）
        allowed_filter_keys = {"memory_type", "confidence"}
        unknown = set(op.filter or {}) - allowed_filter_keys
        if unknown:
            raise ValueError(
                f"filter 只支持 {sorted(allowed_filter_keys)}，收到: {sorted(unknown)}"
            )

        if op.query:
            results = self.searcher.search(
                op.query, scopes=scopes, k=op.limit + op.offset
            )
        else:
            # 无 query：退化为按 scope 列出（按 id 排序，顺序稳定）
            entries = []
            for scope in scopes or [e.scope for e in self.store.list()]:
                entries.extend(self.store.list(scope))
            entries = sorted({e.id: e for e in entries}.values(), key=lambda e: e.id)
            results = None
        items: list[SearchItem] = []
        source_entries = (
            [r.entry for r in results] if results is not None else list(entries)
        )
        scores = (
            {r.entry.id: r.score for r in results} if results is not None else {}
        )
        for entry in source_entries:
            if op.filter:
                if op.filter.get("memory_type") and entry.memory_type != op.filter["memory_type"]:
                    continue
                if op.filter.get("confidence") and entry.confidence != op.filter["confidence"]:
                    continue
            items.append(
                SearchItem(
                    namespace=scope_to_namespace(entry.scope),
                    key=entry.id,
                    value=_entry_value(entry),
                    created_at=datetime.combine(entry.created_at, time.min),
                    updated_at=datetime.combine(entry.last_verified, time.min),
                    score=scores.get(entry.id),
                )
            )
        return items[op.offset : op.offset + op.limit]

    def _handle_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        scopes = sorted({e.scope for e in self.store.list()})
        namespaces = [scope_to_namespace(s) for s in scopes]
        for cond in op.match_conditions:
            if cond.match_type == "prefix":
                namespaces = [
                    ns for ns in namespaces if ns[: len(cond.path)] == tuple(cond.path)
                ]
            elif cond.match_type == "suffix":
                namespaces = [
                    ns for ns in namespaces if ns[-len(cond.path) :] == tuple(cond.path)
                ]
        if op.max_depth is not None:
            namespaces = sorted({ns[: op.max_depth] for ns in namespaces})
        return namespaces[op.offset : op.offset + op.limit]
