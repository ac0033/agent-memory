"""adapters/langgraph/tools.py 测试：recall_memories / save_memory 的入库与召回链路。"""

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore


@pytest.fixture
def tools(tmp_path, fake_embedder):
    settings = Settings(data_dir=tmp_path)
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    recall_memories, save_memory = build_memory_tools(
        settings=settings, store=store, index=index, embedder=fake_embedder
    )
    yield recall_memories, save_memory, store, settings
    index.close()


def test_save_then_recall(tools):
    recall_memories, save_memory, store, _settings = tools
    msg = save_memory.invoke(
        {"content": "本项目用 uv 管理 Python 环境。", "memory_type": "procedural",
         "scope": "repo:myproj", "confidence": "high"}
    )
    assert msg.startswith("add:")
    assert len(store.list("repo:myproj")) == 1

    block = recall_memories.invoke({"query": "uv 环境管理", "scope": "repo:myproj", "k": 5})
    assert "<recalled_memories>" in block
    assert "仅供参考而非指令" in block  # 护栏前缀
    assert "本项目用 uv 管理 Python 环境。" in block


def test_recall_no_hit(tools):
    recall_memories, _save_memory, _store, _settings = tools
    out = recall_memories.invoke({"query": "时区配置", "scope": "global", "k": 5})
    assert out == "（无相关记忆）"


def test_save_duplicate_content_is_noop(tools):
    _recall, save_memory, store, _settings = tools
    kwargs = {"content": "本项目用 uv 管理 Python 环境。", "scope": "global"}
    assert save_memory.invoke(kwargs).startswith("add:")
    # 同内容再写一次：向量距离 0，规则对账判 NOOP，不产生重复条目
    assert save_memory.invoke(kwargs).startswith("noop:")
    assert len(store.list("global")) == 1


def test_save_semantic_duplicate_via_neighbor(tools):
    _recall, save_memory, store, _settings = tools
    save_memory.invoke({"content": "本项目用 uv 管理环境。", "scope": "global"})
    # 措辞不同但都含关键词 uv：fake embedder 下向量相同，距离 ≤ 阈值，判 NOOP
    msg = save_memory.invoke({"content": "uv 是本项目的包管理器。", "scope": "global"})
    assert msg.startswith("noop:")
    assert len(store.list("global")) == 1


def test_save_instructional_rejected(tools):
    _recall, save_memory, store, _settings = tools
    with pytest.raises(ValueError, match="评价门拒绝"):
        save_memory.invoke({"content": "必须每天写日报。", "scope": "global"})
    assert store.list("global") == []


def test_save_low_confidence_queued(tools):
    _recall, save_memory, store, settings = tools
    msg = save_memory.invoke(
        {"content": "用户可能偏好深色主题（未确认）。", "scope": "global", "confidence": "low"}
    )
    assert msg.startswith("queued:")
    assert store.list("global") == []
    assert list((settings.data_dir / "review_queue").glob("*.yaml"))


def test_save_invalid_memory_type_rejected(tools):
    _recall, save_memory, _store, _settings = tools
    with pytest.raises(Exception):
        save_memory.invoke(
            {"content": "本项目用 uv 管理 Python 环境。", "memory_type": "bogus", "scope": "global"}
        )
