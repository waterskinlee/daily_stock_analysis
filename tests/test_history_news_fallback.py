# -*- coding: utf-8 -*-
"""Tests for history fallback published_date hard filtering (Issue #697)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json

from src.services.history_service import HistoryService


class HistoryNewsFallbackTestCase(unittest.TestCase):
    def test_resolve_news_prefers_record_snapshot_over_batch_query_rows(self) -> None:
        content = (
            "【百合花 603823 股票 最新消息 搜索结果】\n\n"
            "1. 【公司资讯】百合花：光刻胶仅占营收0.084% (2026-08-17)\n"
            "百合花新闻摘要\n"
            "关联度: direct_company_news; score=91"
        )
        record = SimpleNamespace(
            query_id="batch-q",
            code="603823",
            name="百合花",
            created_at=datetime.now(),
            context_snapshot=json.dumps({"news_retrieval_content": content}),
            news_content=content,
        )
        mixed = SimpleNamespace(
            code="002709",
            name="天赐材料",
            title="天赐材料新闻",
            snippet="不应出现在百合花报告",
            url="https://example.com/tian-ci",
        )
        mock_db = MagicMock()
        mock_db.get_analysis_history_by_id.return_value = record
        mock_db.get_news_intel_by_query_id.return_value = [mixed]

        items = HistoryService(mock_db).resolve_and_get_news("432", limit=8)

        self.assertEqual([item["title"] for item in items], ["百合花：光刻胶仅占营收0.084%"])
        self.assertEqual(items[0]["snippet"], "百合花新闻摘要")
        self.assertEqual(items[0]["url"], "")
        mock_db.get_news_intel_by_query_id.assert_not_called()

    def test_fallback_uses_supplied_record_and_filters_wrong_stock_name(self) -> None:
        now = datetime.now()
        analysis = SimpleNamespace(code="603823", name="百合花", created_at=now)
        candidates = [
            SimpleNamespace(
                code="603823",
                name="禾望电气",
                fetched_at=now,
                published_date=now,
                title="错配新闻",
            ),
            SimpleNamespace(
                code="603823",
                name="百合花",
                fetched_at=now,
                published_date=now,
                title="百合花新闻",
            ),
        ]
        mock_db = MagicMock()
        mock_db.get_recent_news.return_value = candidates

        fake_cfg = SimpleNamespace(news_max_age_days=30, news_strategy_profile="short")
        with patch("src.services.history_service.get_config", return_value=fake_cfg):
            result = HistoryService(mock_db)._fallback_news_by_analysis_context(analysis, limit=20)

        self.assertEqual([item.title for item in result], ["百合花新闻"])
        mock_db.get_analysis_history.assert_not_called()

    def test_fallback_filters_by_published_date_window(self) -> None:
        now = datetime.now()
        analysis = SimpleNamespace(code="600519", created_at=now)

        # All entries are within fetched_at window; only one should pass published_date window.
        candidates = [
            SimpleNamespace(
                fetched_at=now,
                published_date=now - timedelta(days=20),  # too old
                title="old",
            ),
            SimpleNamespace(
                fetched_at=now,
                published_date=None,  # unknown -> drop
                title="unknown",
            ),
            SimpleNamespace(
                fetched_at=now,
                published_date=now - timedelta(days=1),  # valid
                title="fresh",
            ),
        ]

        mock_db = MagicMock()
        mock_db.get_analysis_history.return_value = [analysis]
        mock_db.get_recent_news.return_value = candidates

        svc = HistoryService(db_manager=mock_db)
        fake_cfg = SimpleNamespace(news_max_age_days=30, news_strategy_profile="short")
        with patch("src.services.history_service.get_config", return_value=fake_cfg):
            result = svc._fallback_news_by_analysis_context(analysis, limit=20)

        self.assertEqual([item.title for item in result], ["fresh"])

    def test_fallback_uses_analysis_date_as_window_anchor(self) -> None:
        analysis_time = datetime.now() - timedelta(days=40)
        analysis = SimpleNamespace(code="600519", created_at=analysis_time)

        candidates = [
            SimpleNamespace(
                fetched_at=analysis_time,
                published_date=analysis_time - timedelta(days=10),  # too old for short profile
                title="too_old_for_analysis_window",
            ),
            SimpleNamespace(
                fetched_at=analysis_time,
                published_date=analysis_time - timedelta(days=1),  # valid around analysis date
                title="valid_near_analysis_date",
            ),
        ]

        mock_db = MagicMock()
        mock_db.get_analysis_history.return_value = [analysis]
        mock_db.get_recent_news.return_value = candidates

        svc = HistoryService(db_manager=mock_db)
        fake_cfg = SimpleNamespace(news_max_age_days=30, news_strategy_profile="short")
        with patch("src.services.history_service.get_config", return_value=fake_cfg):
            result = svc._fallback_news_by_analysis_context(analysis, limit=20)

        self.assertEqual([item.title for item in result], ["valid_near_analysis_date"])


if __name__ == "__main__":
    unittest.main()
