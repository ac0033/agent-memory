"""FTS5（trigram）查询表达式构造：记忆层与原文归档层共用。

为什么需要分词：两个索引的 FTS5 都用 `tokenize='trigram'`（`unicode61` 做不到
中文子串匹配），trigram 下 `MATCH '"<原文>"'` 等价于**整句子串匹配**。对 agent
发来的关键词式短查询（"uv"、"commit 格式"）没问题，但对自然语言长问句
（"Where did I get my guitar serviced?"）几乎恒为 0 命中——稀疏路等于失效，
混合检索退化成纯向量。所以要把整句短语和其中的词项一起作为候选表达式。

`raw_index` 自 v0.2 就是这么做的（多表达式），`index_db` 不是；本模块把这段
逻辑收敛成一份，两边共用，避免两套写法继续漂移。

返回的表达式已做 FTS5 双引号转义，调用方直接作为 `MATCH ?` 的参数即可；
每个表达式单独查询、按首次出现顺序合并，排名靠前的表达式优先——这与 RRF
只吃排名、不吃分数的口径一致。
"""

from __future__ import annotations

import re

# trigram 分词器要求匹配串至少 3 个字符，更短的词项不会命中
MIN_TERM_CHARS = 3
# 词项上限：长问句切出的词太多会把候选池灌满噪声，也拖慢查询
DEFAULT_MAX_TERMS = 12
# RRF 常数，与 hybrid / raw_index 的融合口径一致
RRF_K = 60

_SPLIT_RE = re.compile(r"[\s，。、；：？！,.;:?!（）()“”\"'【】\[\]{}<>/\|~`@#$%^&*+=_-]+")


def split_terms(text: str) -> list[str]:
    """按空白与中英文标点切词，保留非空片段（长度过滤由调用方决定）。"""
    return [t for t in _SPLIT_RE.split(text) if t]


def _escape(text: str) -> str:
    """FTS5 字符串字面量转义：内部的双引号翻倍。"""
    return text.replace('"', '""')


def fts_expressions(query: str, max_terms: int = DEFAULT_MAX_TERMS) -> list[str]:
    """把查询变成一串 FTS5 MATCH 表达式：整句短语在前，词项在后。

    返回列表已去重且保序；query 去掉首尾空白后短于 MIN_TERM_CHARS 时返回空列表
    （trigram 不可能命中，调用方应当直接跳过稀疏路，让稠密路兜底）。
    """
    q = query.strip()
    if len(q) < MIN_TERM_CHARS:
        return []
    exprs = [f'"{_escape(q)}"']
    seen = {q}
    for term in split_terms(q):
        if len(term) < MIN_TERM_CHARS or term in seen:
            continue
        seen.add(term)
        exprs.append(f'"{_escape(term)}"')
        if len(exprs) > max_terms:  # 1 个整句 + max_terms 个词项
            break
    return exprs


def fuse_ranked_lists(rankings: list[list], rrf_k: int = RRF_K) -> list:
    """把多个表达式各自的排序结果融合成一个排序（RRF，与检索层同一套公式）。

    为什么不能按"表达式顺序依次拼接"：表达式的顺序就是词项在问句里出现的顺序，
    而不是词项的信息量。"Where did I get my guitar serviced?" 里 "Where"、"did"
    命中成百上千行，会把候选位置占满，真正有区分度的 "guitar" 反而挤不进来。
    RRF 融合让"命中多个查询词项的行"自然累积更高的分——整句短语命中的行同时也
    命中每个词项，因此拿到最高分，不需要再额外加权。

    同一 id 在一个 ranking 里重复出现时只计首次排名。
    """
    fused: dict = {}
    for ranking in rankings:
        seen: set = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen:
                continue
            seen.add(key)
            fused[key] = fused.get(key, 0.0) + 1.0 / (rrf_k + rank)
    return [key for key, _ in sorted(fused.items(), key=lambda kv: -kv[1])]


def fts_or_query(query: str, max_terms: int = DEFAULT_MAX_TERMS) -> str | None:
    """把整句短语和各词项用 OR 拼成**一个** FTS5 查询，交给 `bm25()` 一次排序。

    为什么不再"每个词项各查一次、再按名次等权融合"：名次融合没有 IDF——问句里的
    "with""that" 和 "guitar" 各出一个排名、权重一样，虚词命中的行会和真正相关的行平起平坐。
    FTS5 的 bm25() 对 OR 查询按各短语的 IDF 加权求和，这才是标准 BM25 的口径
    （朴素 RAG 基线用的就是它）。实测：改之前，朴素 RAG 取到的前 20 条原话只有 57% 也在
    我们的原文路前 20 里。

    查询短于 MIN_TERM_CHARS 时返回 None（trigram 不可能命中，由稠密路兜底）。
    """
    exprs = fts_expressions(query, max_terms)
    return " OR ".join(exprs) if exprs else None


_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")


def has_cjk(text: str) -> bool:
    return _CJK_RE.search(text) is not None


def words_or_query(query: str) -> str | None:
    """词级（unicode61）表用的 OR 查询：全部拉丁/数字词元，不限长、不截数。

    与 trigram 表不同，词级表不需要 3 字符下限；"I""my""is" 这类词 IDF 很低但不是零，
    朴素 RAG 的 BM25 就是这样给几乎每一行一个稀疏名次的。原文路要与它对等（原则一），
    就不能让没命中长词的行在稀疏路里没有名次。
    """
    terms = list(dict.fromkeys(t.lower() for t in _WORD_RE.findall(query)))
    return " OR ".join(f'"{t}"' for t in terms) if terms else None
