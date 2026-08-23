"""adapters/langgraph/store.py 测试：AgentMemoryStore 的 namespace→scope 映射与 CRUD。

全部走 fake embedder，不依赖真实模型。同步 API（get/search/put/delete）在
langgraph 1.x 里都是 batch 的语法糖，所以测同步入口即覆盖了 batch 分发。
"""

import asyncio

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.adapters.langgraph.store import (
    AgentMemoryStore,
    namespace_to_scope,
    scope_to_namespace,
)
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore

NS_GLOBAL = ("memories", "global")
NS_REPO = ("memories", "repo:myproj")


@pytest.fixture
def ams(tmp_path, fake_embedder):
    settings = Settings(data_dir=tmp_path)
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    ams = AgentMemoryStore(settings=settings, store=store, index=index, embedder=fake_embedder)
    yield ams
    index.close()


# ---------------------------------------------------------------- namespace 映射


def test_namespace_to_scope_valid():
    assert namespace_to_scope(("memories", "global")) == "global"
    assert namespace_to_scope(("memories", "repo:myproj")) == "repo:myproj"
    assert scope_to_namespace("agent:kimi") == ("memories", "agent:kimi")


@pytest.mark.parametrize(
    "ns",
    [("global",), ("memories",), ("memory", "global"), ("memories", "global", "extra"), ()],
)
def test_namespace_to_scope_rejects_bad_shape(ns):
    with pytest.raises(ValueError, match="namespace"):
        namespace_to_scope(ns)


# ---------------------------------------------------------------- put / get / delete


def test_put_and_get_roundtrip(ams):
    ams.put(NS_GLOBAL, "uv-env", {"content": "本项目用 uv 管理 Python 环境。"})
    item = ams.get(NS_GLOBAL, "uv-env")
    assert item is not None
    assert item.key == "uv-env"
    assert item.namespace == NS_GLOBAL
    assert item.value["content"] == "本项目用 uv 管理 Python 环境。"
    assert item.value["memory_type"] == "semantic"  # 默认类型
    assert item.value["scope"] == "global"
    # 记忆层与索引都落了
    assert ams.store.get("uv-env").content == "本项目用 uv 管理 Python 环境。"
    assert ams.index.get_meta("uv-env") is not None


def test_get_missing_returns_none(ams):
    assert ams.get(NS_GLOBAL, "no-such-id") is None


def test_put_upsert_bumps_version_and_keeps_created_at(ams):
    ams.put(NS_GLOBAL, "uv-env", {"content": "本项目用 uv 管理 Python 环境。"})
    ams.put(NS_GLOBAL, "uv-env", {"content": "本项目用 uv 管理 Python 环境，包管理走 uv.lock。"})
    entry = ams.store.get("uv-env")
    assert entry.version == 2  # update 自动 +1
    assert entry.content == "本项目用 uv 管理 Python 环境，包管理走 uv.lock。"
    assert ams.index.count() == 1  # upsert 不产生重复索引


def test_put_explicit_fields(ams):
    ams.put(
        NS_REPO,
        "commit-style",
        {"content": "本项目 commit message 用中文。", "memory_type": "procedural",
         "confidence": "high", "source": "my-agent"},
    )
    entry = ams.store.get("commit-style")
    assert entry.scope == "repo:myproj"
    assert entry.memory_type == "procedural"
    assert entry.confidence == "high"
    assert entry.source == "my-agent"


def test_put_instructional_content_rejected(ams):
    # D2：指令性内容过评价门被拒，fail-closed 抛错
    with pytest.raises(ValueError, match="评价门拒绝"):
        ams.put(NS_GLOBAL, "bad-entry", {"content": "必须每次提交前跳舞庆祝。"})
    with pytest.raises(Exception):
        ams.store.get("bad-entry")


def test_put_low_confidence_queued_not_stored(ams):
    ams.put(
        NS_GLOBAL, "shaky-guess",
        {"content": "用户可能偏好深色主题（未确认）。", "confidence": "low"},
    )
    assert ams.get(NS_GLOBAL, "shaky-guess") is None
    queue_files = list((ams.settings.data_dir / "review_queue").glob("*shaky-guess*.yaml"))
    assert len(queue_files) == 1


def test_put_redacts_secrets(ams):
    ams.put(NS_GLOBAL, "api-endpoint", {"content": "内部网关 api_key=abcd1234efgh5678 走内网。"})
    entry = ams.store.get("api-endpoint")
    assert "abcd1234efgh5678" not in entry.content
    assert "[REDACTED:api_key]" in entry.content


def test_delete_via_put_none(ams):
    ams.put(NS_GLOBAL, "uv-env", {"content": "本项目用 uv 管理 Python 环境。"})
    ams.delete(NS_GLOBAL, "uv-env")
    assert ams.get(NS_GLOBAL, "uv-env") is None
    assert ams.index.get_meta("uv-env") is None


def test_delete_missing_fails_closed(ams):
    with pytest.raises(KeyError):
        ams.delete(NS_GLOBAL, "no-such-id")


def test_scope_mismatch_on_upsert_rejected(ams):
    # scope 不允许通过写路径变更（MarkdownStore.update 的硬规则）
    ams.put(NS_GLOBAL, "uv-env", {"content": "本项目用 uv 管理 Python 环境。"})
    with pytest.raises(Exception, match="scope"):
        ams.put(NS_REPO, "uv-env", {"content": "本项目用 uv 管理 Python 环境，升级版。"})


# ---------------------------------------------------------------- search


def test_search_hybrid_and_scope_isolation(ams, fake_embedder):
    ams.put(NS_GLOBAL, "uv-env", {"content": "用 uv 管理环境是全局约定。"})
    ams.put(NS_REPO, "port-entry", {"content": "myproj 的 dev server 端口固定 8765。"})

    hits = ams.search(NS_REPO, query="uv")
    ids = {h.key for h in hits}
    assert "uv-env" in ids  # repo scope 检索自动并入 global
    assert hits[0].key == "uv-env"  # 语义最相关的排第一（稠密路 kNN 不做距离截断）

    # repo:other 的上下文不应命中 repo:myproj 的条目
    hits_other = ams.search(("memories", "repo:other"), query="端口")
    assert "port-entry" not in {h.key for h in hits_other}


def test_search_namespace_root_searches_all_scopes(ams):
    ams.put(NS_REPO, "port-entry", {"content": "myproj 的 dev server 端口固定 8765。"})
    hits = ams.search(("memories",), query="端口")
    assert "port-entry" in {h.key for h in hits}


def test_search_filter_and_unknown_filter_key(ams):
    ams.put(NS_GLOBAL, "uv-env", {"content": "用 uv 管理环境是全局约定。",
                                  "memory_type": "procedural", "confidence": "high"})
    hits = ams.search(NS_GLOBAL, query="uv", filter={"memory_type": "procedural"})
    assert [h.key for h in hits] == ["uv-env"]
    hits = ams.search(NS_GLOBAL, query="uv", filter={"memory_type": "episodic"})
    assert hits == []
    with pytest.raises(ValueError, match="filter"):
        ams.search(NS_GLOBAL, query="uv", filter={"owner": "me"})


def test_search_limit_offset(ams):
    for i in range(3):
        ams.put(NS_GLOBAL, f"uv-note-{i}", {"content": f"第 {i} 条 uv 使用约定记录。"})
    all_hits = ams.search(NS_GLOBAL, query="uv", limit=10)
    page = ams.search(NS_GLOBAL, query="uv", limit=2, offset=1)
    assert len(all_hits) == 3
    assert [h.key for h in page] == [h.key for h in all_hits][1:3]


# ---------------------------------------------------------------- list_namespaces / 异步


def test_list_namespaces(ams):
    ams.put(NS_GLOBAL, "uv-env", {"content": "用 uv 管理环境是全局约定。"})
    ams.put(NS_REPO, "port-entry", {"content": "myproj 的 dev server 端口固定 8765。"})
    namespaces = ams.list_namespaces(prefix=("memories",))
    assert NS_GLOBAL in namespaces and NS_REPO in namespaces


def test_abatch_async_roundtrip(ams):
    async def run():
        await ams.abatch([])  # 空批
        # 通过同步 put 造数据，再走异步 get/search 验证 abatch 通路
        ams.put(NS_GLOBAL, "uv-env", {"content": "用 uv 管理环境是全局约定。"})
        item = await ams.aget(NS_GLOBAL, "uv-env")
        hits = await ams.asearch(NS_GLOBAL, query="uv")
        return item, hits

    item, hits = asyncio.run(run())
    assert item is not None and item.key == "uv-env"
    assert [h.key for h in hits] == ["uv-env"]


def test_put_rejects_bad_namespace(ams):
    with pytest.raises(ValueError, match="namespace"):
        ams.put(("wrong", "global"), "x", {"content": "用 uv 管理环境是全局约定。"})
