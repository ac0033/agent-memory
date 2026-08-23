"""mcp_server.py 测试：MemoryService 五个 tool 的业务行为（直接调 handler，不走传输）。"""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from agent_memory.config import Settings
from agent_memory.server.mcp_server import MemoryService, build_server


class FakeLLM:
    """蒸馏返回固定 JSON 的 fake。"""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or {"memories": []}

    def complete(self, system: str, user: str) -> str:
        return "ok"

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        return self.payload


@pytest.fixture
def service(tmp_path, fake_embedder):
    from agent_memory.long_term.store.index_db import IndexDB
    from agent_memory.long_term.store.markdown_store import MarkdownStore

    settings = Settings(data_dir=tmp_path)
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    svc = MemoryService(settings, store, index, fake_embedder, FakeLLM())
    yield svc
    index.close()


def _add_direct(service, entry_id="port-entry", content="本项目 dev server 端口固定 8765。"):
    return service.add(content=content, entry_id=entry_id, scope="global")


def test_add_direct_and_search(service):
    report = _add_direct(service)
    assert report["mode"] == "direct"
    assert report["reconcile"]["add"] == 1

    out = service.search("这个项目端口是多少", scope="global", k=5)
    assert "<recalled_memories>" in out["block"]
    assert out["hits"][0]["id"] == "port-entry"


def test_add_conversation_distill_pipeline(service):
    service.llm = FakeLLM(
        {
            "memories": [
                {
                    "id": "db-sqlite",
                    "content": "本项目数据库为 SQLite，文件路径 data/dev.db。",
                    "memory_type": "semantic",
                    "confidence": "high",
                    "evidence_turns": [1, 2],
                }
            ]
        }
    )
    conversation = json.dumps(
        [
            {"role": "user", "content": "数据库用 SQLite，文件在 data/dev.db。"},
            {"role": "assistant", "content": "收到。"},
        ]
    )
    report = service.add(conversation_json=conversation, scope="global", session_id="s1")
    assert report["mode"] == "distill"
    assert report["distilled"] == 1
    assert report["reconcile"]["add"] == 1
    assert service.store.get("db-sqlite").evidence[0].session_id == "s1"


def test_add_conversation_accepts_list(service):
    """容错：调用方把对话作为原生数组而非 JSON 字符串传来时，服务端代为序列化。"""
    service.llm = FakeLLM(
        {
            "memories": [
                {
                    "id": "db-sqlite",
                    "content": "本项目数据库为 SQLite，文件路径 data/dev.db。",
                    "memory_type": "semantic",
                    "confidence": "high",
                    "evidence_turns": [1],
                }
            ]
        }
    )
    conversation = [
        {"role": "user", "content": "数据库用 SQLite，文件在 data/dev.db。"},
        {"role": "assistant", "content": "收到。"},
    ]
    report = service.add(conversation_json=conversation, scope="global", session_id="s1")
    assert report["mode"] == "distill"
    assert report["distilled"] == 1


def test_add_conversation_rejects_bad_type(service):
    """既不是字符串也不是数组的类型：报错并提示正确格式。"""
    service.llm = FakeLLM({"memories": []})
    with pytest.raises(ValueError, match="conversation_json 格式错误"):
        service.add(conversation_json={"role": "user", "content": "你好"})


def test_add_conversation_rejects_invalid_json_string(service):
    """字符串但不是合法 JSON：报错并提示期望格式。"""
    service.llm = FakeLLM({"memories": []})
    with pytest.raises(ValueError, match="不是合法 JSON"):
        service.add(conversation_json="这不是 JSON")


def test_add_conversation_without_llm_fails_closed(tmp_path, fake_embedder):
    from agent_memory.llm import LLMError
    from agent_memory.long_term.store.index_db import IndexDB
    from agent_memory.long_term.store.markdown_store import MarkdownStore

    settings = Settings(data_dir=tmp_path)
    svc = MemoryService(
        settings, MarkdownStore(tmp_path), IndexDB(tmp_path / "index.db"), fake_embedder, llm=None
    )
    with pytest.raises(LLMError):
        svc.add(conversation_json='[{"role": "user", "content": "你好"}]')


def test_search_scope_enforced(service):
    """scope 过滤在检索层强制：repo 下的记忆不会出现在 global 检索里。"""
    service.add(
        content="trading-api 仓库 dev server 端口固定 8765。",
        entry_id="repo-scoped",
        scope="repo:trading-api",
    )
    out = service.search("端口是多少", scope="global", k=5)
    assert all(h["id"] != "repo-scoped" for h in out["hits"])
    out2 = service.search("端口是多少", scope="repo:trading-api", k=5)
    assert any(h["id"] == "repo-scoped" for h in out2["hits"])


def test_search_scope_normalized(service):
    """旧写法下划线 scope 归一化后检索：不再静默返回空结果。"""
    service.add(
        content="llm-wiki 仓库用 MkDocs 构建文档。",
        entry_id="wiki-stack",
        scope="repo:llm-wiki",
    )
    out = service.search("文档怎么构建", scope="repo:llm_wiki", k=5)
    assert any(h["id"] == "wiki-stack" for h in out["hits"])


def test_add_scope_normalized(service):
    """写路径与检索同口径归一化：下划线 scope 落到连字符命名空间。"""
    service.add(content="用户偏好 neovim。", entry_id="editor-pref", scope="repo:llm_wiki")
    assert service.store.get("editor-pref").scope == "repo:llm-wiki"


def test_search_invalid_scope_fails_loudly(service):
    """归一化后仍非法的 scope 当场报错，不静默返回空。"""
    with pytest.raises(ValueError, match="scope"):
        service.search("随便查查", scope="not a scope")


def test_feedback_raises_and_lowers_confidence(service):
    _add_direct(service)
    # high → 已见顶，再 helpful 仍 high
    out = service.feedback("port-entry", helpful=True)
    assert out["confidence"] == "high"
    out = service.feedback("port-entry", helpful=False)
    assert out["confidence"] == "medium"
    out = service.feedback("port-entry", helpful=False)
    assert out["confidence"] == "low"
    # low 再降：移出正式库，进复核队列
    out = service.feedback("port-entry", helpful=False, note="这条没用")
    assert out["action"] == "queued_for_review"
    with pytest.raises(KeyError):
        service.store.get("port-entry")
    payload = yaml.safe_load(Path(out["queue_file"]).read_text(encoding="utf-8"))
    assert payload["entry"]["id"] == "port-entry"
    assert "这条没用" in payload["reason"]


def test_feedback_missing_entry_fails_closed(service):
    with pytest.raises(KeyError):
        service.feedback("not-exist", helpful=True)


def test_update_passes_redact_and_gate(service):
    _add_direct(service)
    out = service.update("port-entry", "本项目 dev server 端口已改为 8766。")
    assert out["version"] == 2
    assert "8766" in service.store.get("port-entry").content
    # 指令性内容被评价门拒绝
    with pytest.raises(ValueError, match="评价门拒绝"):
        service.update("port-entry", "以后都要先跑测试再提交代码。")


def test_forget_deletes(service):
    _add_direct(service)
    out = service.forget("port-entry")
    assert out["action"] == "deleted"
    with pytest.raises(KeyError):
        service.store.get("port-entry")
    with pytest.raises(KeyError):
        service.forget("port-entry")  # 再删一次 fail-closed


def test_build_server_registers_all_tools(service):
    server = build_server(service)
    names = {t.name for t in server._tool_manager._tools.values()} if hasattr(
        server, "_tool_manager"
    ) else set()
    # mcp 2.0 的 MCPServer 内部结构可能变化；宽松验证：能 build 且不报错
    assert server is not None
    if names:
        assert {
            "memory_search",
            "memory_add",
            "memory_feedback",
            "memory_update",
            "memory_forget",
            "memory_review_list",
            "memory_review_resolve",
            "memory_wm_read",
            "memory_wm_write",
            "memory_wm_clear",
            "memory_context",
            "memory_transcript_read",
        } <= names


# ---------------------------------------------------------------- M5 人工复核交互


def _make_service(tmp_path, fake_embedder, review_gate="ask"):
    from agent_memory.long_term.store.index_db import IndexDB
    from agent_memory.long_term.store.markdown_store import MarkdownStore

    settings = Settings(data_dir=tmp_path, review_gate=review_gate)
    index = IndexDB(tmp_path / "index.db")
    svc = MemoryService(
        settings, MarkdownStore(tmp_path), index, fake_embedder, FakeLLM()
    )
    return svc, index


def _queue_low_entry(service, entry_id="low-entry"):
    """通过低置信度写入制造一条复核待办，返回报告。"""
    return service.add(
        content="项目明年可能迁移到 PostgreSQL。",
        entry_id=entry_id,
        scope="global",
        confidence="low",
    )


def test_add_direct_pending_review_details(service):
    report = _queue_low_entry(service)
    assert report["gate_queued"] == ["low-entry"]
    assert len(report["pending_review"]) == 1
    item = report["pending_review"][0]
    assert item["id"] == "low-entry"
    assert "PostgreSQL" in item["content_preview"]
    assert "confidence=low" in item["reason"]
    # 低置信度条目不进正式库
    with pytest.raises(KeyError):
        service.store.get("low-entry")


def test_search_ok_reports_zero_pending(service):
    _add_direct(service)
    out = service.search("端口是多少", scope="global")
    assert out["status"] == "ok"
    assert out["pending_review_count"] == 0
    assert "<recalled_memories>" in out["block"]


def test_search_blocked_by_gate_ask(service):
    _queue_low_entry(service)
    out = service.search("数据库", scope="global")
    assert out["status"] == "blocked"
    assert out["gate"] == "ask"
    assert out["pending_review_count"] == 1
    assert "hits" not in out  # 拦截时不返回任何记忆内容
    # 用户确认后放行
    out2 = service.search("数据库", scope="global", acknowledge_pending=True)
    assert out2["status"] == "ok"
    assert out2["pending_review_count"] == 1


def test_search_gate_off_never_blocks(tmp_path, fake_embedder):
    svc, index = _make_service(tmp_path, fake_embedder, review_gate="off")
    try:
        _queue_low_entry(svc)
        out = svc.search("数据库", scope="global")
        assert out["status"] == "ok"
        assert out["pending_review_count"] == 1
    finally:
        index.close()


def test_search_gate_strict_blocks_even_with_ack(tmp_path, fake_embedder):
    svc, index = _make_service(tmp_path, fake_embedder, review_gate="strict")
    try:
        _queue_low_entry(svc)
        out = svc.search("数据库", scope="global", acknowledge_pending=True)
        assert out["status"] == "blocked"
        assert out["gate"] == "strict"
        assert "hits" not in out
    finally:
        index.close()


def test_review_list(service):
    _queue_low_entry(service)
    out = service.review_list()
    assert out["pending_review_count"] == 1
    item = out["items"][0]
    assert item["entry"]["id"] == "low-entry"
    assert item["file"].endswith(".yaml")
    assert not item["unreadable"]


def test_review_resolve_approve(service):
    _queue_low_entry(service)
    queue_file = service.review_list()["items"][0]["file"]
    out = service.review_resolve(queue_file, "approve")
    assert out["action"] == "approved"
    entry = service.store.get("low-entry")
    assert entry.confidence == "low"  # 人工确认后按原样入库
    assert entry.last_verified == date.today()  # 裁决视为已核实
    assert service.review_list()["pending_review_count"] == 0
    # 入库后可被检索到
    assert service.search("PostgreSQL", scope="global")["status"] == "ok"


def test_review_resolve_modify(service):
    _queue_low_entry(service)
    queue_file = service.review_list()["items"][0]["file"]
    out = service.review_resolve(
        queue_file, "modify", new_content="项目已确定 2027 年迁移到 PostgreSQL。"
    )
    assert out["action"] == "modified"
    assert "已确定" in service.store.get("low-entry").content
    assert service.review_list()["pending_review_count"] == 0


def test_review_resolve_modify_passes_gate(service):
    _queue_low_entry(service)
    queue_file = service.review_list()["items"][0]["file"]
    with pytest.raises(ValueError, match="评价门拒绝"):
        service.review_resolve(queue_file, "modify", new_content="以后都要先跑测试再提交。")
    with pytest.raises(ValueError, match="new_content"):
        service.review_resolve(queue_file, "modify")


def test_review_resolve_discard(service):
    _queue_low_entry(service)
    queue_file = service.review_list()["items"][0]["file"]
    out = service.review_resolve(queue_file, "discard")
    assert out["action"] == "discarded"
    assert service.review_list()["pending_review_count"] == 0
    with pytest.raises(KeyError):
        service.store.get("low-entry")


def test_review_resolve_raw_record_cannot_approve(tmp_path, fake_embedder):
    from agent_memory.long_term.ingest.review_queue import write_review_queue_raw

    svc, index = _make_service(tmp_path, fake_embedder)
    try:
        files = write_review_queue_raw(
            [({"id": "Bad ID!", "content": "x"}, "id 非法")], tmp_path, reason="蒸馏产出非法"
        )
        with pytest.raises(ValueError, match="raw_record"):
            svc.review_resolve(files[0].name, "approve")
        out = svc.review_resolve(files[0].name, "discard")
        assert out["action"] == "discarded"
    finally:
        index.close()


def test_review_resolve_fail_closed(service):
    with pytest.raises(ValueError, match="非法 action"):
        service.review_resolve("whatever.yaml", "maybe")
    with pytest.raises(FileNotFoundError):
        service.review_resolve("not-exist.yaml", "approve")
    with pytest.raises(ValueError, match="非法队列文件名"):
        service.review_resolve("../outside.yaml", "approve")


# ---------------------------------------------------------------- M6 作用域纪律


def test_add_without_scope_returns_reminder(service):
    report = service.add(content="用户的开发机是 Windows。", entry_id="dev-os")
    assert report["scope_reminder"] is not None
    assert "global" in report["scope_reminder"]
    assert service.store.get("dev-os").scope == "global"  # 缺省回落 global


def test_add_with_explicit_scope_no_reminder(service):
    report = _add_direct(service)  # _add_direct 显式传了 scope="global"
    assert report["scope_reminder"] is None
    report2 = service.add(
        content="trading-api 仓库用 uv 管理依赖。",
        entry_id="uv-repo",
        scope="repo:trading-api",
    )
    assert report2["scope_reminder"] is None


# ---------------------------------------------------------------- M7a 工作记忆


def test_wm_write_and_read_roundtrip(service):
    out = service.wm_write(
        "repo:demo",
        goal="完成 M7a",
        decisions=["工作记忆全量替换"],
        variables={"预算": "1000"},
        todos=[{"content": "补测试", "status": "pending"}, "跑验收"],
        notes=["文档后补"],
        turn_watermark=3,
    )
    assert out["status"] == "ok"
    assert out["version"] == 1
    assert out["redacted_fields"] == 0

    read = service.wm_read("repo:demo", current_turn=3)
    assert read["exists"] is True
    assert read["stale_wm"] is False  # 轮次等于水位不算滞后
    assert read["turn_watermark"] == 3
    wm = read["working_memory"]
    assert wm["goal"] == "完成 M7a"
    assert wm["todos"][0] == {"content": "补测试", "status": "pending"}
    assert wm["todos"][1]["content"] == "跑验收"  # 纯字符串按 pending
    assert wm["todos"][1]["status"] == "pending"
    assert "## 工作记忆（当前任务状态）" in read["block"]
    # 再读一次：轮次超过水位则 stale
    assert service.wm_read("repo:demo", current_turn=5)["stale_wm"] is True


def test_wm_read_missing_scope(service):
    out = service.wm_read("repo:nothing")
    assert out["exists"] is False
    assert out["block"] == ""
    assert out["stale_wm"] is False
    assert out["working_memory"] is None


def test_wm_write_redacts(service):
    out = service.wm_write(
        "global",
        goal="排查密钥泄露，联系 admin@example.com",
        todos=["api_key=abcdef1234567890 先撤销"],
    )
    assert out["redacted_fields"] == 2  # goal 的 email + todo 的 api_key
    read = service.wm_read("global")
    assert "admin@example.com" not in read["working_memory"]["goal"]
    assert "[REDACTED:email]" in read["working_memory"]["goal"]
    assert "[REDACTED:api_key]" in read["working_memory"]["todos"][0]["content"]


def test_wm_write_full_replace_semantics(service):
    service.wm_write("global", goal="第一版", decisions=["决策甲"], notes=["备注甲"])
    out = service.wm_write("global", goal="第二版")  # 未传字段 = 置空，不是合并
    assert out["version"] == 2
    wm = service.wm_read("global")["working_memory"]
    assert wm["goal"] == "第二版"
    assert wm["decisions"] == [] and wm["notes"] == [] and wm["todos"] == []


def test_wm_write_keeps_watermark_when_not_given(service):
    service.wm_write("global", goal="v1", turn_watermark=7)
    service.wm_write("global", goal="v2")
    assert service.wm_read("global")["turn_watermark"] == 7


def test_wm_invalid_scope_fails_loudly(service):
    with pytest.raises(ValueError, match="scope"):
        service.wm_read("not a scope")
    with pytest.raises(ValueError, match="scope"):
        service.wm_write("not a scope", goal="x")
    with pytest.raises(ValueError, match="scope"):
        service.wm_clear("not a scope")


def test_wm_clear_idempotent(service):
    service.wm_write("global", goal="x")
    assert service.wm_clear("global")["status"] == "ok"
    assert service.wm_read("global")["exists"] is False
    # 本就不存在：幂等意图，返回 already empty 而不是报错
    out = service.wm_clear("global")
    assert out["status"] == "ok"
    assert out["note"] == "already empty"


# ---------------------------------------------------------------- M7a 统一上下文组装


def _prepare_three_layers(service):
    """造齐三个分节的数据：profile 常驻层 + 工作记忆 + 可召回条目。"""
    service.add(
        content="用户偏好使用中文交流沟通。",
        entry_id="lang-pref",
        scope="global",
        memory_type="profile",
    )
    service.wm_write("global", goal="组装上下文", turn_watermark=2)
    _add_direct(service)  # port-entry，含"端口"关键词供召回


def test_context_assembles_in_order(service):
    _prepare_three_layers(service)
    out = service.context("global", query="端口是多少", current_turn=2)
    assert out["status"] == "ok"
    sections = out["sections"]
    assert "## 长期记忆（用户画像）" in sections["profile"]
    assert "## 工作记忆（当前任务状态）" in sections["working_memory"]
    assert "<recalled_memories>" in sections["recall"]
    # block = 三个非空分节按框架顺序拼接：profile > 工作记忆 > 召回
    block = out["block"]
    assert block.index("## 长期记忆（用户画像）") < block.index("## 工作记忆（当前任务状态）")
    assert block.index("## 工作记忆（当前任务状态）") < block.index("<recalled_memories>")
    assert out["stale_wm"] is False
    assert service.context("global", query="端口是多少", current_turn=9)["stale_wm"] is True


def test_context_without_query_skips_recall(service):
    _prepare_three_layers(service)
    out = service.context("global")
    assert out["sections"]["recall"] == ""
    assert "<recalled_memories>" not in out["block"]
    assert out["sections"]["profile"] and out["sections"]["working_memory"]


def test_context_empty_memory_empty_block(service):
    out = service.context("global", query="端口是多少")
    assert out["status"] == "ok"
    assert out["block"] == ""
    assert out["pending_review_count"] == 0


def test_context_passes_through_gate_blocked(service):
    _prepare_three_layers(service)
    _queue_low_entry(service)  # 制造复核积压（默认 review_gate=ask）
    out = service.context("global", query="端口是多少")
    # 复核门语义与 memory_search 一致：blocked 原样透出，不返回任何记忆内容
    assert out["status"] == "blocked"
    assert out["gate"] == "ask"
    assert "sections" not in out
    # 用户确认后放行
    out2 = service.context("global", query="端口是多少", acknowledge_pending=True)
    assert out2["status"] == "ok"
    assert out2["pending_review_count"] == 1


def test_wm_scope_normalized(service):
    """scope 与检索同口径归一化：下划线写法落到连字符命名空间。"""
    service.wm_write("repo:llm_wiki", goal="x")
    assert service.wm_read("repo:llm-wiki")["exists"] is True


# ---------------------------------------------------------------- M7b 短期记忆 transcript 读取


def _write_wire(tmp_path, records, name="wire.jsonl"):
    """造一个小型 wire.jsonl（格式细节见 tests/test_transcript_adapter.py）。"""
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def _wire_records():
    return [
        {"type": "metadata", "protocol_version": "1.5"},
        {
            "type": "context.append_message",
            "message": {"role": "user", "content": [{"type": "text", "text": "第一轮"}]},
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "turnId": "0",
                "part": {"type": "text", "text": "回复一"},
            },
        },
        {"type": "turn.ended", "turnId": 0},
        {
            "type": "context.append_message",
            "message": {"role": "user", "content": [{"type": "text", "text": "第二轮"}]},
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "turnId": "1",
                "part": {"type": "text", "text": "回复二"},
            },
        },
    ]


def test_transcript_read_auto_detect(service, tmp_path):
    path = _write_wire(tmp_path, _wire_records())
    out = service.transcript_read(str(path))
    assert out["status"] == "ok"
    assert out["adapter"] == "kimi-code-wire"
    assert out["turn_count"] == 4
    assert [(t["role"], t["turn_index"]) for t in out["turns"]] == [
        ("user", 0),
        ("assistant", 0),
        ("user", 1),
        ("assistant", 1),
    ]


def test_transcript_read_since_turn_incremental(service, tmp_path):
    """since_turn 是"水位之后"的增量语义：只返回 turn_index > since_turn 的轮次。"""
    path = _write_wire(tmp_path, _wire_records())
    out = service.transcript_read(str(path), since_turn=0)
    assert out["turn_count"] == 2
    assert [(t["role"], t["turn_index"]) for t in out["turns"]] == [
        ("user", 1),
        ("assistant", 1),
    ]
    out = service.transcript_read(str(path), since_turn=1)
    assert out["turn_count"] == 0


def test_transcript_read_explicit_adapter(service, tmp_path):
    """文件名识别不了时显式指定 adapter 仍可解析。"""
    path = _write_wire(tmp_path, _wire_records(), name="session.log")
    out = service.transcript_read(str(path), adapter="kimi-code-wire")
    assert out["status"] == "ok"
    assert out["turn_count"] == 4


def test_transcript_read_fail_closed(service, tmp_path):
    path = _write_wire(tmp_path, _wire_records(), name="session.log")
    with pytest.raises(ValueError, match="显式指定 adapter"):
        service.transcript_read(str(path))  # 文件名识别不了且不指定 adapter
    with pytest.raises(ValueError, match="未知的 transcript 适配器"):
        service.transcript_read(str(path), adapter="not-exist")
    with pytest.raises(FileNotFoundError):
        service.transcript_read(str(tmp_path / "wire.jsonl"))
