"""agent-memory 命令行入口（typer）。

命令：add / distill / search / list / update / forget / rebuild / stats。
数据目录由 AGENT_MEMORY_DATA_DIR 或默认 ~/.agent-memory/data 决定。
写入前一律过 redact 脱敏（红线 D2），命中敏感信息时警告并写入脱敏后文本。
distill 走完整写入管线：蒸馏 → 评价门 → 对账，需要配置 LLM。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import typer

from agent_memory.config import Settings, get_settings
from agent_memory.llm import LLMError, OpenAILLMClient
from agent_memory.long_term.ingest.distill import distill_memories, parse_conversation_json
from agent_memory.long_term.ingest.gate import gate_candidates
from agent_memory.long_term.ingest.reconcile import reconcile
from agent_memory.long_term.ingest.redact import redact
from agent_memory.long_term.retrieve.embedder import get_embedder
from agent_memory.long_term.retrieve.hybrid import HybridSearcher
from agent_memory.long_term.store.coordinator import MemoryWriter
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore, MemoryStoreError
from agent_memory.models import MemoryEntry

app = typer.Typer(help="agent-memory：本地长期记忆基础设施 CLI", no_args_is_help=True)


def _get_embedder():
    """取默认 Embedder（lazy）。独立成函数便于测试 monkeypatch。"""
    return get_embedder()


@contextmanager
def _components() -> Iterator[tuple[Settings, MarkdownStore, IndexDB]]:
    """构建运行组件；退出时关闭索引连接（Windows 下不关会锁住 index.db）。"""
    settings = get_settings()
    store = MarkdownStore(settings.data_dir)
    index = IndexDB(settings.data_dir / "index.db")
    try:
        yield settings, store, index
    finally:
        index.close()


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


@app.command()
def add(
    entry_id: str = typer.Option(..., "--id", help="kebab-case 记忆 id"),
    content: str | None = typer.Option(None, "--content", help="记忆正文（一句话事实）"),
    detail: str | None = typer.Option(
        None, "--detail", help="可选：带前因后果的完整段落（2-4 句，≤800 字符）"
    ),
    memory_type: str = typer.Option(
        "semantic", "--type", help="semantic/procedural/episodic/profile"
    ),
    scope: str = typer.Option("global", "--scope", help="global | repo:<slug> | agent:<name>"),
    confidence: str = typer.Option("high", "--confidence", help="high/medium/low"),
    source: str = typer.Option("cli", "--source", help="来源标识"),
):
    """新增一条记忆（手动蒸馏：内容由调用方直接写好）。入库前过脱敏。"""
    if content is None:
        content = typer.prompt("记忆内容")
    redacted, hits = redact(content)
    redacted_detail: str | None = None
    if detail:
        redacted_detail, detail_hits = redact(detail)
        hits = hits + detail_hits
    if hits:
        typer.secho(
            f"警告：内容命中敏感信息 {hits}，已替换为 [REDACTED:类型] 后入库",
            fg=typer.colors.YELLOW,
            err=True,
        )
    today = date.today()
    with _components() as (settings, store, index):
        try:
            entry = MemoryEntry(
                id=entry_id,
                content=redacted,
                detail=redacted_detail,
                memory_type=memory_type,
                scope=scope,
                confidence=confidence,
                source=source,
                created_at=today,
                last_verified=today,
            )
            gate_result = gate_candidates([entry], settings.data_dir)
            if gate_result.rejected:
                raise ValueError(f"评价门拒绝 add：{gate_result.rejected[0][1]}")
            if gate_result.queued:
                typer.secho("低置信度候选已进入人工复核队列，未写入正式库", err=True)
                return
            MemoryWriter(store, index, _get_embedder()).create(entry)
        except (ValueError, RuntimeError, MemoryStoreError) as e:
            raise _fail(f"入库失败：{e}") from e
    typer.echo(f"已入库：{entry.id}（scope={entry.scope}, type={entry.memory_type}）")


@app.command()
def distill(
    file: Path = typer.Option(..., "--file", help="对话 JSON 文件（[{role, content}, ...]）"),
    scope: str = typer.Option("global", "--scope", help="global | repo:<slug> | agent:<name>"),
    source: str = typer.Option("cli", "--source", help="来源标识"),
    session_id: str | None = typer.Option(None, "--session-id", help="证据指针的会话 id"),
):
    """把一段对话蒸馏入库：蒸馏 → 评价门 → 对账（红线 D2 完整写入管线）。"""
    settings = get_settings()
    try:
        llm = OpenAILLMClient.from_settings(settings)
    except LLMError as e:
        raise _fail(str(e)) from e
    try:
        conversation = parse_conversation_json(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _fail(f"对话文件读取失败：{e}") from e

    session_id = session_id or f"cli-{datetime.now():%Y%m%dT%H%M%S}"
    distill_result = distill_memories(
        conversation, scope, source, session_id, llm, data_dir=settings.data_dir
    )
    typer.echo(
        f"蒸馏产出 {len(distill_result.entries)} 条候选"
        f"（id 规范化 {len(distill_result.normalized_ids)}，"
        f"非法进复核队列 {len(distill_result.invalid_records)}，"
        f"脱敏丢弃 {distill_result.dropped_redacted}）"
    )
    for raw_id, new_id in distill_result.normalized_ids.items():
        typer.secho(f"  id 规范化：{raw_id!r} -> {new_id!r}", fg=typer.colors.YELLOW)
    for path in distill_result.queued_files:
        typer.secho(f"  非法产出进复核队列：{path.name}", fg=typer.colors.YELLOW)
    with _components() as (_, store, index):
        gate_result = gate_candidates(distill_result.entries, settings.data_dir)
        for entry, reason in gate_result.rejected:
            typer.secho(f"  评价门拒绝 {entry.id}：{reason}", fg=typer.colors.YELLOW)
        for entry in gate_result.queued:
            typer.secho(f"  低置信度进复核队列：{entry.id}", fg=typer.colors.YELLOW)
        report = reconcile(
            gate_result.passed, store, index, llm,
            embedder=_get_embedder(), settings=settings,
        )
    counts = report.counts()
    typer.echo(
        "对账结果："
        + ", ".join(f"{k}={v}" for k, v in counts.items())
    )
    for entry, reason in report.queued:
        typer.secho(f"  进复核队列 {entry.id}：{reason}", fg=typer.colors.YELLOW, err=True)


@app.command()
def search(
    query: str,
    scope: list[str] | None = typer.Option(None, "--scope", help="当前 scope，可多次传入"),
    k: int = typer.Option(10, "--k", help="返回条数"),
):
    """混合检索（稠密 + 全文 → RRF 融合）。"""
    with _components() as (settings, store, index):
        searcher = HybridSearcher(store, index, _get_embedder(), settings)
        results = searcher.search(query, scopes=scope, k=k, track_retrieval=True)
    if not results:
        typer.echo("无命中。")
        return
    for i, r in enumerate(results, start=1):
        typer.echo(
            f"[{i}] {r.entry.id} (score={r.score:.4f}, scope={r.entry.scope},"
            f" confidence={r.entry.confidence})"
        )
        typer.echo(f"    {r.matched_text}")


@app.command()
def evolve(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只到提案为止：产出并打印提案摘要，不验证、不应用"
    ),
    scope: str | None = typer.Option(None, "--scope", help="只整理某个 scope（默认全库）"),
):
    """睡眠学习循环：触发 → 整合 → 验证 → 修剪。提案三档验证全过才晋升。"""
    from agent_memory.long_term.evolve.cycle import run_evolution_cycle

    settings = get_settings()
    try:
        llm = OpenAILLMClient.from_settings(settings)
    except LLMError as e:
        raise _fail(str(e)) from e
    report = run_evolution_cycle(
        settings, llm, embedder=_get_embedder(), scope=scope, dry_run=dry_run
    )
    if not report.triggered:
        typer.echo(f"未触发整理：{report.reason}")
        return
    typer.echo(f"触发整理：{report.reason}")
    proposal = report.proposal
    assert proposal is not None
    typer.echo(f"提案 {proposal.id}（{len(proposal.changes)} 条变更）→ {report.proposal_dir}")
    for c in proposal.changes:
        typer.echo(f"  [{c.kind}] {', '.join(c.target_ids)}：{c.reason}")
    if report.dry_run:
        typer.echo("dry-run：已停在提案，未验证、未应用。")
        return
    assert report.verify is not None
    v = report.verify
    tiers = (("boundary", v.boundary), ("retention", v.retention), ("safety", v.safety))
    for tier_name, tier in tiers:
        mark = "通过" if tier.passed else "否决"
        typer.echo(f"  验证 {tier_name}: {mark}（{tier.reason}）")
    if report.applied:
        typer.echo(
            f"已晋升：应用 {len(report.apply.applied)} 条变更，"
            f"跳过人工裁决项 {len(report.apply.skipped)} 条，快照 {report.apply.snapshot_id}"
        )
    else:
        typer.secho("三档验证未全过，提案已归档到 review_queue/evolution/ 交人工，记忆层未改动。",
                    fg=typer.colors.YELLOW)


@app.command(name="list")
def list_cmd(
    scope: str | None = typer.Option(None, "--scope", help="只列某个 scope"),
):
    """列出记忆条目。"""
    with _components() as (_, store, _index):
        entries = store.list(scope=scope)
    if not entries:
        typer.echo("（空）")
        return
    for e in entries:
        typer.echo(
            f"{e.id}\t{e.scope}\t{e.memory_type}\t{e.confidence}\tv{e.version}\t{e.content}"
        )


@app.command()
def update(
    entry_id: str,
    content: str | None = typer.Option(None, "--content", help="新的记忆正文"),
    confidence: str | None = typer.Option(None, "--confidence", help="新的置信度"),
):
    """更新一条记忆（version 自动 +1，last_verified 置为今天）。"""
    with _components() as (_, store, index):
        try:
            entry = store.get(entry_id)
        except KeyError:
            raise _fail(f"找不到记忆 {entry_id!r}") from None
        changes = {}
        if content is not None:
            redacted, hits = redact(content)
            if hits:
                typer.secho(
                    f"警告：内容命中敏感信息 {hits}，已脱敏", fg=typer.colors.YELLOW, err=True
                )
            changes["content"] = redacted
        if confidence is not None:
            changes["confidence"] = confidence
        if not changes:
            raise _fail("没有要更新的字段（--content / --confidence）")
        try:
            candidate = MemoryEntry.model_validate(
                entry.model_dump(mode="python") | changes
            )
            gate_result = gate_candidates([candidate])
            if gate_result.rejected:
                raise ValueError(f"评价门拒绝 update：{gate_result.rejected[0][1]}")
            updated = MemoryWriter(store, index, _get_embedder()).update(candidate)
        except (ValueError, RuntimeError, MemoryStoreError) as e:
            raise _fail(f"更新失败：{e}") from e
    typer.echo(f"已更新：{updated.id}（version={updated.version}）")


@app.command()
def forget(entry_id: str):
    """删除一条记忆（Markdown 层与索引同步删除）。"""
    with _components() as (_, store, index):
        try:
            MemoryWriter(store, index, _get_embedder()).delete(entry_id)
        except KeyError:
            raise _fail(f"找不到记忆 {entry_id!r}") from None
    typer.echo(f"已删除：{entry_id}")


@app.command()
def rebuild():
    """从 Markdown 记忆层全量重建索引（index.db 是派生物，可随时重建）。"""
    with _components() as (settings, store, index):
        n = index.rebuild_from_markdown(store.memory_dir, _get_embedder())
    typer.echo(f"重建完成：{n} 条记忆（{settings.data_dir / 'index.db'}）")


@app.command()
def stats():
    """统计信息：条目数、scope 分布、数据目录。"""
    with _components() as (settings, store, index):
        entries = store.list()
        indexed = index.count()
    by_scope: dict[str, int] = {}
    for e in entries:
        by_scope[e.scope] = by_scope.get(e.scope, 0) + 1
    typer.echo(f"数据目录：{settings.data_dir}")
    typer.echo(f"记忆条目：{len(entries)}（索引内 {indexed} 条）")
    for scope, n in sorted(by_scope.items()):
        typer.echo(f"  {scope}: {n}")
