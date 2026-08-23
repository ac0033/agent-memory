"""混合检索：稠密（vec0 cosine knn）+ 稀疏（FTS5 bm25）→ RRF 融合 → 治理加权。

融合公式（M1）：
- 两路各取 top 20，RRF（k=60）：rrf_score = Σ 1/(60 + rank)，rank 从 1 起；
- 综合分 = rrf_score × confidence_weight × time_decay；
  - confidence_weight：high 1.0 / medium 0.8 / low 0.6；
  - time_decay：last_verified 距今 ≤ stale_days 为 1.0，之后线性衰减，
    到 2×stale_days 时到下限 0.5，不再更低（旧记忆降权但不消失）。
- rerank 是消融开关（settings.rerank_enabled），M1 只留接口，显式开启会
  抛 NotImplementedError（fail-closed，不静默跳过）。
"""

from dataclasses import dataclass
from datetime import date

from agent_memory.config import Settings, get_settings
from agent_memory.models import MemoryEntry
from agent_memory.store.index_db import IndexDB
from agent_memory.store.markdown_store import MarkdownStore

# 两路召回各自取的候选数，之后 RRF 融合
CANDIDATE_TOP_N = 20
# RRF 常数
RRF_K = 60
# time_decay 下限：旧记忆降权但不归零
DECAY_FLOOR = 0.5

_CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.8, "low": 0.6}


def confidence_weight(confidence: str) -> float:
    return _CONFIDENCE_WEIGHTS[confidence]


def time_decay(last_verified: date, today: date, stale_days: int) -> float:
    """按 last_verified 距今天数线性衰减：stale_days 内为 1.0，之后衰减到 0.5 下限。"""
    days = (today - last_verified).days
    if days <= stale_days:
        return 1.0
    horizon = max(stale_days, 1)  # 再过 stale_days 天到达下限
    ratio = min((days - stale_days) / horizon, 1.0)
    return 1.0 - (1.0 - DECAY_FLOOR) * ratio


def rrf_fuse(
    dense_ids: list[str], sparse_ids: list[str], rrf_k: int = RRF_K
) -> dict[str, dict]:
    """RRF 融合两路排序结果。

    返回 {id: {"rrf_score": float, "dense_rank": int|None, "sparse_rank": int|None}}，
    rank 从 1 起，未命中某路为 None。
    """
    fused: dict[str, dict] = {}

    def add(ids: list[str], key: str) -> None:
        for rank, entry_id in enumerate(ids, start=1):
            slot = fused.setdefault(
                entry_id, {"rrf_score": 0.0, "dense_rank": None, "sparse_rank": None}
            )
            slot["rrf_score"] += 1.0 / (rrf_k + rank)
            slot[key] = rank

    add(dense_ids, "dense_rank")
    add(sparse_ids, "sparse_rank")
    return fused


@dataclass
class SearchResult:
    """一条检索结果：完整条目 + 命中文本（原始证据，不做摘要）+ 打分明细。"""

    entry: MemoryEntry
    score: float  # 综合分（rrf × 置信度 × 时间衰减）
    rrf_score: float
    dense_rank: int | None
    sparse_rank: int | None
    matched_text: str  # 命中的原始记忆文本


class HybridSearcher:
    """组合 MarkdownStore（取完整条目）与 IndexDB（两路召回）的检索器。"""

    def __init__(
        self,
        store: MarkdownStore,
        index: IndexDB,
        embedder,
        settings: Settings | None = None,
    ):
        self.store = store
        self.index = index
        self.embedder = embedder
        self.settings = settings or get_settings()

    def search(
        self,
        query: str,
        scopes: list[str] | None = None,
        k: int = 10,
        rerank: bool | None = None,
        track_retrieval: bool = False,
    ) -> list[SearchResult]:
        """混合检索。scopes 为当前 scope 列表，global 会自动并入。

        track_retrieval=True 时给返回条目的 retrieval_count +1（供 M4a 整理循环
        识别长期未被检索的记忆）。默认关闭：写路径的近邻检索（reconcile /
        propagate）不是用户检索，不应计数。
        """
        if rerank is None:
            rerank = self.settings.rerank_enabled
        if rerank:
            raise NotImplementedError("reranker 在 M1 未实现（rerank_enabled 是 M2+ 的消融开关）")

        effective_scopes = self._with_global(scopes)
        dense = self.index.search_dense(
            self.embedder.embed_texts([query])[0], k=CANDIDATE_TOP_N, scopes=effective_scopes
        )
        sparse = self.index.search_sparse(query, k=CANDIDATE_TOP_N, scopes=effective_scopes)
        fused = rrf_fuse([i for i, _ in dense], [i for i, _ in sparse])

        today = date.today()
        results = []
        for entry_id, slot in fused.items():
            entry = self.store.get(entry_id)
            score = (
                slot["rrf_score"]
                * confidence_weight(entry.confidence)
                * time_decay(entry.last_verified, today, self.settings.stale_days)
            )
            results.append(
                SearchResult(
                    entry=entry,
                    score=score,
                    rrf_score=slot["rrf_score"],
                    dense_rank=slot["dense_rank"],
                    sparse_rank=slot["sparse_rank"],
                    matched_text=entry.content,
                )
            )
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:k]
        if track_retrieval:
            for r in results:
                self.store.increment_retrieval_count(r.entry.id)
        return results

    @staticmethod
    def _with_global(scopes: list[str] | None) -> list[str] | None:
        """当前 scope 之外始终并入 global；None 表示不过滤（管理面场景）。"""
        if scopes is None:
            return None
        return list(dict.fromkeys([*scopes, "global"]))
