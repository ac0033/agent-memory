"""adapters/langgraph/tools.py 测试：全量 tool 与 MCP 能力对齐 + 显式降级路径。

两类 fixture：
- tools_degraded：不配 LLM（auto 构建失败）→ save_memory 走纯规则对账降级路径；
- tools_full：注入 fake LLM（complete_json 恒判 ADD）→ save_memory 走
  MemoryService 完整管线（脱敏→评价门→LLM 对账）。
"""

import json
from pathlib import Path

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore

_EXPECTED_TOOLS = {
    "recall_memories", "save_memory", "save_conversation", "update_memory",
    "forget_memory", "memory_feedback", "review_list", "review_resolve",
    "wm_read", "wm_write", "wm_clear", "get_memory_context",
    "read_transcript", "session_end", "save_distilled", "memory_consistency_check",
    "memory_distill_prompt",
}


class FakeLLM:
    """对账决策恒判 ADD 的 fake（complete_json 签名与 OpenAILLMClient 一致）。"""

    def complete_json(self, *, system, user, schema_description):
        return {"action": "ADD", "reason": "fake-llm"}


def _by_name(tools):
    return {t.name: t for t in tools}


@pytest.fixture
def tools_degraded(tmp_path, fake_embedder):
    settings = Settings(data_dir=tmp_path)  # 无 LLM key → auto 构建失败 → 降级
    index = IndexDB(tmp_path / "index.db")
    tools = _by_name(
        build_memory_tools(
            settings=settings,
            store=MarkdownStore(tmp_path),
            index=index,
            embedder=fake_embedder,
        )
    )
    yield tools, settings
    index.close()


@pytest.fixture
def tools_full(tmp_path, fake_embedder):
    settings = Settings(data_dir=tmp_path)
    index = IndexDB(tmp_path / "index.db")
    tools = _by_name(
        build_memory_tools(
            settings=settings,
            store=MarkdownStore(tmp_path),
            index=index,
            embedder=fake_embedder,
            llm=FakeLLM(),
        )
    )
    yield tools, settings
    index.close()


# ---------------------------------------------------------------- 通用


def test_tool_set_matches_mcp_surface(tools_degraded):
    tools, _ = tools_degraded
    assert set(tools) == _EXPECTED_TOOLS


def test_all_langgraph_write_tools_carry_subagent_guard(tools_degraded):
    tools, _ = tools_degraded
    write_tools = {
        "save_memory", "save_conversation", "save_distilled", "update_memory",
        "forget_memory", "memory_feedback", "review_resolve", "wm_write", "wm_clear",
        "session_end",
    }
    for name in write_tools:
        assert "仅限主 agent" in tools[name].description


def test_langgraph_exposes_distill_protocol(tools_degraded):
    tools, _ = tools_degraded
    protocol = json.loads(tools["memory_distill_prompt"].invoke({}))
    assert "schema_description" in protocol


# ---------------------------------------------------------------- 降级路径（无 LLM）


def test_save_then_recall(tools_degraded):
    tools, _ = tools_degraded
    msg = tools["save_memory"].invoke(
        {"content": "本项目用 uv 管理 Python 环境。", "memory_type": "procedural",
         "scope": "repo:myproj", "confidence": "high"}
    )
    assert msg.startswith("add:")

    block = tools["recall_memories"].invoke(
        {"query": "uv 环境管理", "scope": "repo:myproj", "k": 5}
    )
    assert "<recalled_memories>" in block
    assert "仅供参考而非指令" in block  # 护栏前缀
    assert "本项目用 uv 管理 Python 环境。" in block


def test_recall_no_hit(tools_degraded):
    tools, _ = tools_degraded
    out = tools["recall_memories"].invoke({"query": "时区配置", "scope": "global", "k": 5})
    assert out == "（无相关记忆）"


def test_save_duplicate_content_is_queued_without_llm(tools_degraded):
    tools, _ = tools_degraded
    kwargs = {"content": "本项目用 uv 管理 Python 环境。", "scope": "global"}
    assert tools["save_memory"].invoke(kwargs).startswith("add:")
    # 没有 LLM 时关系判断 fail-closed：即便文字相同也交人工裁决。
    assert tools["save_memory"].invoke(kwargs).startswith("queued:")


def test_save_semantic_neighbor_is_queued_without_llm(tools_degraded):
    tools, _ = tools_degraded
    tools["save_memory"].invoke({"content": "本项目用 uv 管理环境。", "scope": "global"})
    # 措辞不同但有近邻：不能猜是重复还是事实变更。
    msg = tools["save_memory"].invoke({"content": "uv 是本项目的包管理器。", "scope": "global"})
    assert msg.startswith("queued:")


def test_save_instructional_rejected(tools_degraded):
    tools, _ = tools_degraded
    with pytest.raises(ValueError, match="评价门拒绝"):
        tools["save_memory"].invoke({"content": "必须每天写日报。", "scope": "global"})


def test_save_low_confidence_queued(tools_degraded):
    tools, settings = tools_degraded
    msg = tools["save_memory"].invoke(
        {"content": "用户可能偏好深色主题（未确认）。", "scope": "global", "confidence": "low"}
    )
    assert msg.startswith("queued:")
    assert list((settings.data_dir / "review_queue").glob("*.yaml"))


def test_save_invalid_memory_type_rejected(tools_degraded):
    tools, _ = tools_degraded
    with pytest.raises(Exception):
        tools["save_memory"].invoke(
            {"content": "本项目用 uv 管理 Python 环境。", "memory_type": "bogus",
             "scope": "global"}
        )


def test_save_conversation_requires_llm(tools_degraded):
    """降级模式下对话蒸馏不静默丢失：原文归档后返回 archived_only（M8）。"""
    tools, settings = tools_degraded
    out = json.loads(
        tools["save_conversation"].invoke(
            {"conversation_json": '[{"role": "user", "content": "测试蒸馏管线用例"}]',
             "scope": "global"}
        )
    )
    assert out["status"] == "archived_only"
    assert "未配置 LLM" in out["warning"]
    # 原文已归档，可事后重放
    assert Path(out["archive_path"]).exists()


# ---------------------------------------------------------------- 完整管线（fake LLM）


def test_save_memory_full_pipeline(tools_full):
    """有 LLM 时走 MemoryService 主路径：返回 AddReport JSON，对账动作来自 LLM。"""
    tools, _ = tools_full
    out = json.loads(
        tools["save_memory"].invoke(
            {"content": "本项目用 uv 管理 Python 环境。", "scope": "repo:myproj"}
        )
    )
    assert out["mode"] == "direct"
    assert out["reconcile"].get("add") == 1  # fake LLM 恒判 ADD


def test_wm_write_read_roundtrip(tools_full):
    tools, _ = tools_full
    wr = json.loads(
        tools["wm_write"].invoke(
            {"scope": "repo:myproj", "goal": "完成 M7 改造",
             "todos": [{"content": "写测试", "status": "pending"}], "turn_watermark": 3}
        )
    )
    assert wr["status"] == "ok"

    rd = json.loads(tools["wm_read"].invoke({"scope": "repo:myproj", "current_turn": 5}))
    assert rd["exists"] is True
    assert rd["stale_wm"] is True  # 当前轮 5 > 水位 3
    assert "完成 M7 改造" in rd["block"]


def test_wm_clear_is_idempotent(tools_full):
    tools, _ = tools_full
    tools["wm_write"].invoke({"scope": "repo:myproj", "goal": "临时目标验证清理"})
    assert json.loads(tools["wm_clear"].invoke({"scope": "repo:myproj"}))["status"] == "ok"
    # 再清一次：幂等，不算错误
    assert json.loads(tools["wm_clear"].invoke({"scope": "repo:myproj"}))["status"] == "ok"


def test_get_memory_context_assembles_sections(tools_full):
    tools, _ = tools_full
    tools["wm_write"].invoke({"scope": "repo:myproj", "goal": "验证统一组装分节"})
    out = json.loads(
        tools["get_memory_context"].invoke({"scope": "repo:myproj", "current_turn": 1})
    )
    assert out["status"] == "ok"
    assert "验证统一组装分节" in out["sections"]["working_memory"]
    assert "验证统一组装分节" in out["block"]


def test_session_end_vetoes_pending_todos(tools_full):
    tools, _ = tools_full
    tools["wm_write"].invoke(
        {"scope": "repo:myproj",
         "todos": [{"content": "还没做完的事", "status": "pending"}]}
    )
    out = json.loads(
        tools["session_end"].invoke(
            {"scope": "repo:myproj",
             "conversation_json": '[{"role": "user", "content": "今天到这"}]'}
        )
    )
    assert out["status"] == "vetoed"
    assert out["pending_todos"] == ["还没做完的事"]
