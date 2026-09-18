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
