"""FTS5 查询表达式构造（fts_query）：记忆层与原文层共用的分词逻辑。"""

from agent_memory.long_term.store.fts_query import (
    DEFAULT_MAX_TERMS,
    fts_expressions,
    split_terms,
)


class TestSplitTerms:
    def test_splits_on_latin_and_cjk_punctuation(self):
        assert split_terms("commit message, 格式：约定") == ["commit", "message", "格式", "约定"]

    def test_empty_fragments_dropped(self):
        assert split_terms("  a ,,  b  ") == ["a", "b"]


class TestFtsExpressions:
    def test_natural_language_question_yields_phrase_then_terms(self):
        exprs = fts_expressions("Where did I get my guitar serviced?")
        # 整句短语排在最前（最精确），词项依次在后
        assert exprs[0] == '"Where did I get my guitar serviced?"'
        assert '"guitar"' in exprs
        assert '"serviced"' in exprs

    def test_short_terms_below_trigram_floor_are_skipped(self):
        exprs = fts_expressions("Where did I get my guitar serviced?")
        # trigram 要求 >=3 字符，"I"、"my" 不构成表达式
        assert '"I"' not in exprs
        assert '"my"' not in exprs

    def test_query_shorter_than_trigram_floor_yields_nothing(self):
        # 比 trigram 下界还短，任何表达式都不可能命中，交给稠密路兜底
        assert fts_expressions("uv") == []
        assert fts_expressions("记忆") == []

    def test_keyword_query_still_produces_phrase(self):
        assert fts_expressions("uv sync") == ['"uv sync"', '"sync"']

    def test_double_quotes_escaped(self):
        exprs = fts_expressions('他说 "别动这个文件"')
        assert all(e.startswith('"') and e.endswith('"') for e in exprs)
        assert '""' in exprs[0]  # 内部双引号被翻倍

    def test_term_count_capped(self):
        long_q = " ".join(f"term{i:03d}" for i in range(40))
        exprs = fts_expressions(long_q)
        assert len(exprs) == DEFAULT_MAX_TERMS + 1  # 1 个整句 + 上限个词项

    def test_terms_deduplicated_preserving_order(self):
        exprs = fts_expressions("guitar shop guitar repair")
        assert exprs.count('"guitar"') == 1
        assert exprs.index('"guitar"') < exprs.index('"repair"')
