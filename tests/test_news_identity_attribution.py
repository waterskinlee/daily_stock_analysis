# -*- coding: utf-8 -*-
"""Regression tests for news identity/attribution admission rules.

Covers the 2026-08 misattribution class: generic 2-char Chinese short names
(e.g. 中国黄金 -> 黄金) must never establish direct company identity alone,
and stock-news admission must prefer empty over unverifiable fillers.
"""

from __future__ import annotations

import unittest

from src.search_service import SearchResponse, SearchResult, SearchService


def _result(
    title: str,
    *,
    snippet: str = "摘要内容",
    url: str | None = None,
    source: str = "finance.example.com",
) -> SearchResult:
    return SearchResult(
        title=title,
        snippet=snippet,
        url=url if url is not None else f"https://example.com/{abs(hash(title))}",
        source=source,
        published_date=None,
    )


class CompanyIdentityTermsTestCase(unittest.TestCase):
    def test_short_name_not_in_strong_terms(self) -> None:
        terms = SearchService._company_identity_terms("中国黄金", stock_code="600916")
        self.assertIn("中国黄金", terms)
        self.assertNotIn("黄金", terms)

    def test_weak_terms_return_suffix_for_cn_codes(self) -> None:
        weak = SearchService._weak_company_identity_terms("中国黄金", stock_code="600916")
        self.assertEqual(weak, ["黄金"])

    def test_weak_terms_empty_for_non_cn_codes(self) -> None:
        self.assertEqual(
            SearchService._weak_company_identity_terms("腾讯控股", stock_code="00700"),
            [],
        )
        self.assertEqual(SearchService._weak_company_identity_terms("中国黄金"), [])


class ScoreNewsRelevanceTestCase(unittest.TestCase):
    def _score(self, title: str, snippet: str = "普通市场报道") -> SearchResult:
        item = _result(title, snippet=snippet)
        return SearchService._score_news_relevance(
            item,
            stock_code="600916",
            stock_name="中国黄金",
        )

    def test_generic_sector_title_not_direct(self) -> None:
        # 泛词标题（黄金大涨）不得判为 direct_company_news
        scored = self._score("黄金大涨创年内新高", "避险情绪推动金价上行")
        self.assertNotEqual(scored.relevance_category, SearchService._DIRECT_NEWS_CATEGORY)

    def test_other_company_with_generic_word_not_direct(self) -> None:
        scored = self._score("山东黄金发布年度业绩报告", "公司公告全文")
        self.assertNotEqual(scored.relevance_category, SearchService._DIRECT_NEWS_CATEGORY)

    def test_full_name_title_is_direct(self) -> None:
        scored = self._score("中国黄金发布公告", "公司公告全文")
        self.assertEqual(scored.relevance_category, SearchService._DIRECT_NEWS_CATEGORY)

    def test_code_hit_is_direct(self) -> None:
        scored = self._score("这家公司业绩亮眼", "600916 公告披露")
        self.assertEqual(scored.relevance_category, SearchService._DIRECT_NEWS_CATEGORY)


class FilterRankedNewsContextTestCase(unittest.TestCase):
    @staticmethod
    def _noise_response() -> SearchResponse:
        items = [
            _result("黄金ETF资金流入创新高"),
            _result("能源板块走强带动大盘"),
        ]
        for item in items:
            item.relevance_score = 0
            item.relevance_category = "sector_related_news"
        return SearchResponse(query="q", results=items, provider="Mock", success=True)

    def test_strict_mode_returns_empty_when_nothing_verified(self) -> None:
        filtered = SearchService._filter_ranked_news_for_context(
            self._noise_response(),
            log_scope="test:strict",
            drop_unverified_when_empty=True,
        )
        self.assertEqual(filtered.results, [])

    def test_default_mode_keeps_background_items(self) -> None:
        response = self._noise_response()
        filtered = SearchService._filter_ranked_news_for_context(
            response,
            log_scope="test:lenient",
        )
        self.assertEqual(len(filtered.results), 2)

    def test_strict_mode_keeps_positive_relevance(self) -> None:
        response = self._noise_response()
        response.results[0].relevance_score = 45
        response.results[0].relevance_category = SearchService._DIRECT_NEWS_CATEGORY
        filtered = SearchService._filter_ranked_news_for_context(
            response,
            log_scope="test:strict-keep",
            drop_unverified_when_empty=True,
        )
        self.assertEqual(len(filtered.results), 1)
        self.assertEqual(
            filtered.results[0].relevance_category,
            SearchService._DIRECT_NEWS_CATEGORY,
        )


class DedupeComprehensiveIntelTestCase(unittest.TestCase):
    def test_same_url_removed_from_later_dimensions(self) -> None:
        shared_url = "https://news.example.com/article/123"
        latest = SearchResponse(
            query="q",
            results=[
                _result("最新消息一", url=shared_url),
                _result("最新消息二", url="https://news.example.com/other"),
            ],
            provider="Mock",
            success=True,
        )
        earnings = SearchResponse(
            query="q",
            results=[_result("同一篇文章标题不同", url=shared_url + "?from=rss")],
            provider="Mock",
            success=True,
        )
        deduped = SearchService._dedupe_comprehensive_intel(
            {"latest_news": latest, "earnings": earnings}
        )
        self.assertEqual(len(deduped["latest_news"].results), 2)
        self.assertEqual(deduped["earnings"].results, [])

    def test_same_title_without_url_deduplicated(self) -> None:
        a = SearchResponse(
            query="q",
            results=[_result("完全相同的标题", url="")],
            provider="A",
            success=True,
        )
        b = SearchResponse(
            query="q",
            results=[_result("完全 相同 的标题", url="")],
            provider="B",
            success=True,
        )
        deduped = SearchService._dedupe_comprehensive_intel({"d1": a, "d2": b})
        self.assertEqual(deduped["d2"].results, [])
