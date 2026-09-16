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


def test_default_keeps_model_setting(monkeypatch):
    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    e = Embedder("fake")
    e.embed_texts(["a"])
    assert e._model.max_seq_length == 8192
