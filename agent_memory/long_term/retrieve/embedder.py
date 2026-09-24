"""Embedding 封装：BAAI/bge-m3（1024 维，中英双语）。

实现说明：任务原计划用 fastembed 的 TextEmbedding("BAAI/bge-m3")，但 fastembed
（至 0.8.0）的 TextEmbedding 支持列表里没有 bge-m3，所以改用 sentence-transformers
加载同一套模型权重（向量维度和语义空间一致）。

模型首次使用会下载约 2.3GB 到 HF cache（~/.cache/huggingface）。
网络受限时设 HF_ENDPOINT=https://hf-mirror.com 走镜像。
模型加载较慢，模块级单例 lazy 初始化，避免重复加载。
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from agent_memory.config import Settings, get_settings


class Embedder:
    """bge-m3 文本嵌入。lazy 初始化：第一次 embed_texts 时才加载模型。

    线程模型：模型加载与 encode 全部在本实例专属的一个常驻线程里执行，调用方线程只提交
    任务、等结果。原因：torch 在每个**首次调用它的线程**上分配一套线程本地资源（OpenMP
    工作线程与缓冲），线程退出后不归还——Windows 上实测每个短命线程约 10 MB 提交内存。
    对账阶段每次写入都新开线程池并在其中查近邻（reconcile.py），逐题评测一题就泄漏约 4 GB，
    常驻服务也随写入次数缓慢泄漏。单一常驻线程让 torch 只见到一个线程；串行化本来就有
    （推理是 CPU 密集，串行几乎不损失吞吐）。
    """

    def __init__(self, model_name: str | None = None, max_seq_length: int | None = None):
        if model_name is None:
            model_name = get_settings().embedding_model
        self.model_name = model_name
        # None = 模型默认（bge-m3 8192）；见 Settings.embedding_max_seq_length
        self.max_seq_length = max_seq_length
        self._model = None
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedder")

    def _ensure_model(self):
        # 只在工作线程里调用，天然串行，无需加锁
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            if self.max_seq_length:
                self._model.max_seq_length = self.max_seq_length
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        return [v.tolist() for v in model.encode(texts, normalize_embeddings=True)]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入，返回与输入等长的 1024 维向量列表（L2 归一化，适配 cosine）。"""
        if not texts:
            return []
        return self._worker.submit(self._encode, list(texts)).result()


_lock = threading.Lock()
_default: Embedder | None = None


def get_embedder(settings: Settings | None = None) -> Embedder:
    """模块级默认 Embedder 单例（按 settings.embedding_model）。"""
    global _default
    with _lock:
        if _default is None:
            model_name = settings.embedding_model if settings else None
            max_len = settings.embedding_max_seq_length if settings else None
            _default = Embedder(model_name, max_seq_length=max_len)
        return _default
