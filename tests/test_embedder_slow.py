"""真实 embedding 模型的慢测试（默认跳过，uv run pytest -m slow 运行）。

首次运行需要下载约 2GB 的 bge-m3 模型；网络受限时设
HF_ENDPOINT=https://hf-mirror.com 走镜像。
"""

import pytest

from agent_memory.long_term.retrieve.embedder import Embedder
from agent_memory.long_term.store.coordinator import MemoryWriter
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def embedder():
    """Reuse one native model instance; repeated teardown/reload crashes Torch on Windows."""
    return Embedder()


def test_bge_m3_embed_dim_and_batch(embedder):
    vectors = embedder.embed_texts(["用户用 uv 管理环境", "这个项目 dev server 端口是 8765"])
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)


def test_bge_m3_semantic_similarity(embedder):
    q, pos, neg = embedder.embed_texts(
        ["用什么工具管理 Python 环境", "用户所有项目都用 uv 管理环境", "今天天气怎么样"]
    )

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb)

    assert cosine(q, pos) > cosine(q, neg)


def test_bge_m3_consistency_does_not_depend_on_batch_rounding(
    embedder, tmp_path, entry_factory
):
    index = IndexDB(tmp_path / "index.db")
    writer = MemoryWriter(MarkdownStore(tmp_path), index, embedder)
    writer.create(entry_factory(entry_id="fact-a", content="用户确认项目使用 SQLite。"))
    writer.create(entry_factory(entry_id="fact-b", content="用户确认项目使用 uv。"))
    assert writer.check_consistency().consistent
    index.close()
