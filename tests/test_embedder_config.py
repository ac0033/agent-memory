"""Embedder 的最大序列长度上限：来自 Settings，加载模型后写到 SentenceTransformer 上。"""

import numpy as np

from agent_memory.long_term.retrieve.embedder import Embedder


class _FakeST:
    def __init__(self, name):
        self.name = name
        self.max_seq_length = 8192

    def encode(self, texts, normalize_embeddings=True):
        return [np.zeros(4) for _ in texts]


def test_max_seq_length_applied(monkeypatch):
    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    e = Embedder("fake", max_seq_length=512)
    e.embed_texts(["a"])
    assert e._model.max_seq_length == 512


def test_model_runs_on_one_thread_whatever_the_caller(monkeypatch):
    """torch 给每个首次调用它的线程分配资源且不归还：加载与 encode 必须始终在同一个线程。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import sentence_transformers

    seen: set[int] = set()

    class _RecordingST(_FakeST):
        def __init__(self, name):
            seen.add(threading.get_ident())
            super().__init__(name)

        def encode(self, texts, normalize_embeddings=True):
            seen.add(threading.get_ident())
            return super().encode(texts, normalize_embeddings)

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _RecordingST)
    e = Embedder("fake")
    for _ in range(5):  # 每轮新开线程池，模拟对账阶段的写法
        with ThreadPoolExecutor(max_workers=4) as pool:
            out = list(pool.map(lambda t: e.embed_texts([t, t]), "abcd"))
        assert all(len(v) == 2 for v in out)
    assert e.embed_texts([]) == []
    assert len(seen) == 1 and threading.get_ident() not in seen


def test_default_keeps_model_setting(monkeypatch):
    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    e = Embedder("fake")
    e.embed_texts(["a"])
    assert e._model.max_seq_length == 8192
