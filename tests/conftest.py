"""测试共享 fixture：MemoryEntry 工厂与临时 store/index。"""

from datetime import date

import pytest

from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import MemoryEntry


def make_entry(
    entry_id: str = "test-entry",
    content: str = "测试记忆内容：用户的开发机是 Windows。",
    memory_type: str = "semantic",
    scope: str = "global",
    confidence: str = "high",
    last_verified: date | None = None,
    version: int = 1,
) -> MemoryEntry:
    today = date.today()
    return MemoryEntry(
        id=entry_id,
        content=content,
        memory_type=memory_type,
        scope=scope,
        confidence=confidence,
        source="test",
        created_at=today,
        last_verified=last_verified or today,
        version=version,
    )


@pytest.fixture
def entry_factory():
    return make_entry


@pytest.fixture
def store(tmp_path):
    return MarkdownStore(tmp_path)


@pytest.fixture
def index(tmp_path):
    idx = IndexDB(tmp_path / "index.db")
    yield idx
    idx.close()


class FakeEmbedder:
    """确定性假 embedder：把文本映射为可复现的 1024 维向量。

    规则：向量前若干维按文本里是否出现关键词置 1.0，让"语义相近"的文本
    在构造数据里有相近向量，供检索逻辑测试使用，不依赖真实模型。
    """

    KEYWORDS = ["uv", "端口", "Windows", "时区", "commit"]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * 1024
            for i, kw in enumerate(self.KEYWORDS):
                if kw in text:
                    vec[i] = 1.0
            if not any(vec):
                vec[-1] = 1.0  # 无关键词时落到固定维，避免零向量
            vectors.append(vec)
        return vectors


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
