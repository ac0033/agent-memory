"""Embedding 封装：BAAI/bge-m3（1024 维，中英双语）。

实现说明：任务原计划用 fastembed 的 TextEmbedding("BAAI/bge-m3")，但 fastembed
（至 0.8.0）的 TextEmbedding 支持列表里没有 bge-m3，所以改用 sentence-transformers
加载同一套模型权重（向量维度和语义空间一致）。

模型首次使用会下载约 2.3GB 到 HF cache（~/.cache/huggingface）。
网络受限时设 HF_ENDPOINT=https://hf-mirror.com 走镜像。
模型加载较慢，模块级单例 lazy 初始化，避免重复加载。
"""

import threading

from agent_memory.config import Settings, get_settings


class Embedder:
    """bge-m3 文本嵌入。lazy 初始化：第一次 embed_texts 时才加载模型。

    线程安全：评估 runner 用线程池并发跑用例时共享本单例，encode 加锁串行化
    （推理是 CPU 密集，加锁几乎不损失吞吐，只防并发进 native 代码的未知风险）。
    """

    def __init__(self, model_name: str | None = None):
        if model_name is None:
            model_name = get_settings().embedding_model
        self.model_name = model_name
        self._model = None
        self._encode_lock = threading.Lock()

    def _ensure_model(self):
        if self._model is None:
            with self._encode_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入，返回与输入等长的 1024 维向量列表（L2 归一化，适配 cosine）。"""
        if not texts:
            return []
        model = self._ensure_model()
        with self._encode_lock:
            return [v.tolist() for v in model.encode(texts, normalize_embeddings=True)]


_lock = threading.Lock()
_default: Embedder | None = None


def get_embedder(settings: Settings | None = None) -> Embedder:
    """模块级默认 Embedder 单例（按 settings.embedding_model）。"""
    global _default
    with _lock:
        if _default is None:
            model_name = settings.embedding_model if settings else None
            _default = Embedder(model_name)
        return _default
