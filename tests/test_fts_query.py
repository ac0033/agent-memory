"""FTS5 查询表达式构造（fts_query）：记忆层与原文层共用的分词逻辑。"""

from agent_memory.long_term.store.fts_query import (
    DEFAULT_MAX_TERMS,
    fts_expressions,
    fuse_ranked_lists,
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


class TestFuseRankedLists:
    def test_row_matching_more_expressions_wins(self):
        """命中多个查询词项的行应排在只命中一个词项的行之前。"""
        # "guitar" 和 "serviced" 都命中 A；B 只命中高频词 "where" 且排第一
        fused = fuse_ranked_lists([["B"], ["A"], ["A", "B"]])
        assert fused[0] == "A"

    def test_single_ranking_order_preserved(self):
        assert fuse_ranked_lists([["a", "b", "c"]]) == ["a", "b", "c"]

    def test_empty_input(self):
        assert fuse_ranked_lists([]) == []
        assert fuse_ranked_lists([[], []]) == []

    def test_duplicate_within_one_ranking_counted_once(self):
        # 同一 ranking 里重复出现只按首次排名计分，否则一个表达式能刷分
        assert fuse_ranked_lists([["a", "a", "a"], ["b"]]) == ["a", "b"]

    def test_high_frequency_term_cannot_bury_discriminative_term(self):
        """回归：按表达式顺序拼接时，虚词命中会占满前排。

        rankings[0] 是虚词 "where" 的命中（noise0..9 排在前），rankings[1] 是
        "guitar" 的命中（evidence 排第一）。融合后 evidence 必须进入前列。
        """
        noise = [f"noise{i}" for i in range(10)]
        fused = fuse_ranked_lists([noise, ["evidence"]])
        assert fused.index("evidence") <= 1
