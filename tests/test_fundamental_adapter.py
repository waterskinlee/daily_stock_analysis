# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
)

from src.search_service import ClsWireSearchProvider, SearchResult, SearchService


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": [within_ttm],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
                "营业收入同比": [12.0],
                "净利润同比": [9.5],
            }
        )
        forecast_df = pd.DataFrame({"股票代码": ["600519"], "预告": ["预增"]})
        quick_df = pd.DataFrame({"股票代码": ["600519"], "快报": ["快报摘要"]})
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (fin_df, "stock_financial_abstract", []),
                (forecast_df, "stock_yjyg_em", []),
                (quick_df, "stock_yjkb_em", []),
                (dividend_df, "stock_fhps_detail_em", []),
                (None, None, []),
                (None, None, []),
            ],
        ):
            result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    # ---- Eastmoney lockup (限售解禁) direct HTTP --------------------------------

    @staticmethod
    def _lockup_rows():
        today = datetime.now().date()
        history_date = (today - timedelta(days=30)).isoformat()
        upcoming_date = (today + timedelta(days=30)).isoformat()
        return [
            {
                "SECURITY_CODE": "600519",
                "FREE_DATE": f"{history_date} 00:00:00",
                "LIMITED_STOCK_TYPE": "首发原股东限售股份",
                "FREE_SHARES_NUM": "10000000",
                "FREE_RATIO": "1.23",
            },
            {
                "SECURITY_CODE": "600519",
                "FREE_DATE": f"{upcoming_date} 00:00:00",
                "LIMITED_STOCK_TYPE": "股权激励限售股份",
                "FREE_SHARES_NUM": "500000",
                "FREE_RATIO": "0.06",
            },
        ]

    def test_lockup_schedule_ok_with_history_and_upcoming(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = datetime.now().date().isoformat()
        history_date = (datetime.now().date() - timedelta(days=30)).isoformat()
        upcoming_date = (datetime.now().date() + timedelta(days=30)).isoformat()
        rows = self._lockup_rows()
        with patch.object(
            adapter,
            "_em_datacenter_get",
            return_value=rows,
        ) as mock_get:
            result = adapter.get_lockup_schedule("600519", lookback_days=180, forward_days=90)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["history"]), 1)  # future row excluded from history
        self.assertEqual(result["history"][0]["free_date"], history_date)
        self.assertEqual(result["history"][0]["share_type"], "首发原股东限售股份")
        self.assertAlmostEqual(result["history"][0]["free_ratio_pct"], 1.23, places=6)
        self.assertEqual(len(result["upcoming"]), 1)  # past row excluded from upcoming
        self.assertEqual(result["upcoming"][0]["free_date"], upcoming_date)
        self.assertEqual(result["upcoming"][0]["share_type"], "股权激励限售股份")
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["as_of"], today)

    def test_lockup_schedule_fail_open_on_network_error(self) -> None:
        adapter = AkshareFundamentalAdapter()
        with patch.object(
            adapter,
            "_em_datacenter_get",
            side_effect=ConnectionError("reset"),
        ):
            result = adapter.get_lockup_schedule("600519")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["history"], [])
        self.assertEqual(result["upcoming"], [])
        self.assertTrue(any("lockup_history" in e for e in result["errors"]))
        self.assertTrue(any("lockup_upcoming" in e for e in result["errors"]))

    def test_lockup_schedule_empty_status(self) -> None:
        adapter = AkshareFundamentalAdapter()
        with patch.object(adapter, "_em_datacenter_get", return_value=[]):
            result = adapter.get_lockup_schedule("600519")
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["history"], [])
        self.assertEqual(result["upcoming"], [])

    # ---- 同花顺 consensus EPS (一致预期) direct HTTP -----------------------------

    def test_ths_consensus_eps_parses_years(self) -> None:
        adapter = AkshareFundamentalAdapter()
        html = (
            "<html><body><table><thead><tr>"
            "<th>年度</th><th>预测机构数</th><th>最小值</th><th>均值</th><th>最大值</th>"
            "</tr></thead><tbody>"
            "<tr><td>2026年</td><td>24</td><td>6.50</td><td>7.20</td><td>8.10</td></tr>"
            "<tr><td>2027年</td><td>12</td><td>8.00</td><td>9.10</td><td>10.50</td></tr>"
            "</tbody></table></body></html>"
        )
        resp = MagicMock()
        resp.encoding = "gbk"
        resp.text = html
        resp.raise_for_status = MagicMock()
        with patch("data_provider.fundamental_adapter.requests.get", return_value=resp):
            parsed = adapter._ths_consensus_eps("600519")

        self.assertIn("2026", parsed)
        self.assertEqual(parsed["2026"]["count"], 24)
        self.assertAlmostEqual(parsed["2026"]["min"], 6.50, places=6)
        self.assertAlmostEqual(parsed["2026"]["mean"], 7.20, places=6)
        self.assertAlmostEqual(parsed["2026"]["max"], 8.10, places=6)
        self.assertAlmostEqual(parsed["2027"]["mean"], 9.10, places=6)

    def test_ths_consensus_eps_fail_open_on_http_error(self) -> None:
        adapter = AkshareFundamentalAdapter()
        with patch(
            "data_provider.fundamental_adapter.requests.get",
            side_effect=ConnectionError("unreachable"),
        ):
            with self.assertRaises(ConnectionError):
                adapter._ths_consensus_eps("600519")

    # ---- fundamental bundle includes consensus EPS -------------------------------

    def test_fundamental_bundle_includes_consensus_eps(self) -> None:
        adapter = AkshareFundamentalAdapter()
        fin_df = pd.DataFrame({"股票代码": ["600519"], "营业收入同比": [12.0]})
        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (fin_df, "stock_financial_abstract", []),
                (None, None, []),
                (None, None, []),
                (None, None, []),
                (None, None, []),
                (None, None, []),
            ],
        ), patch.object(
            adapter,
            "_ths_consensus_eps",
            return_value={
                "2026": {"count": 24, "min": 6.5, "mean": 7.2, "max": 8.1},
                "2027": {"count": 12, "min": 8.0, "mean": 9.1, "max": 10.5},
            },
        ):
            result = adapter.get_fundamental_bundle("600519")

        consensus = result["earnings"].get("consensus_eps", {})
        self.assertIn("2026", consensus)
        self.assertAlmostEqual(consensus["2026"]["mean"], 7.2, places=6)
        self.assertTrue(any("consensus" in item for item in result["source_chain"]))


class TestClsWireSearchProvider(unittest.TestCase):
    """Tests for CLS telegraph wire provider."""

    @staticmethod
    def _cls_payload(items):
        return {
            "data": {
                "roll_data": [
                    {
                        "id": str(1000 + i),
                        "title": item[0],
                        "brief": item[1],
                        "content": item[1],
                        "ctime": int(item[2]),
                    }
                    for i, item in enumerate(items)
                ]
            }
        }

    @patch("src.search_service.requests.get")
    def test_disabled_provider_not_available(self, mock_get):
        provider = ClsWireSearchProvider(enabled=False)
        self.assertFalse(provider.is_available)

    @patch("src.search_service.requests.get")
    def test_filters_wire_by_query_terms(self, mock_get):
        now_ts = int(datetime.now().timestamp())
        payload = self._cls_payload(
            [
                ("AI算力板块走强", "AI算力板块今日走强，多股涨停", now_ts),
                ("白酒行业动态", "白酒行业消息，与AI无关", now_ts),
                ("央行开展逆回购操作", "央行逆回购操作5000亿", now_ts),
            ]
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        mock_get.return_value = resp

        provider = ClsWireSearchProvider(enabled=True)
        result = provider.search("AI算力", max_results=2, days=7)

        self.assertTrue(result.success)
        self.assertEqual(result.provider, "CLS")
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].source, "财联社")
        self.assertEqual(result.results[0].title, "AI算力板块走强")
        self.assertIsNotNone(result.results[0].published_date)
        self.assertTrue(result.results[0].url.startswith("https://www.cls.cn/"))

    @patch("src.search_service.requests.get")
    def test_falls_back_to_eastmoney_wire_when_cls_404(self, mock_get):
        # CLS dead (404) -> EM 7x24 fallback
        def _side_effect(url, **kwargs):
            if url == ClsWireSearchProvider.CLS_WIRE_URL:
                resp404 = MagicMock()
                resp404.status_code = 404
                return resp404
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "data": {
                    "fastNewsList": [
                        {
                            "code": "202608073835573583",
                            "title": "央行开展逆回购操作5000亿",
                            "summary": "央行今日开展逆回购操作5000亿元。",
                            "showTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        },
                        {
                            "code": "202608073835573584",
                            "title": "白酒板块异动",
                            "summary": "白酒板块盘中异动拉升。",
                            "showTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    ]
                }
            }
            return resp

        mock_get.side_effect = _side_effect
        provider = ClsWireSearchProvider(enabled=True)
        result = provider.search("逆回购", max_results=2, days=7)

        self.assertTrue(result.success)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].source, "东财7x24")
        self.assertEqual(result.results[0].title, "央行开展逆回购操作5000亿")
        self.assertTrue(result.results[0].url.startswith("https://kuaixun.eastmoney.com/"))

    @patch("src.search_service.requests.get")
    def test_fail_open_when_all_wires_down(self, mock_get):
        resp404 = MagicMock()
        resp404.status_code = 404
        mock_get.return_value = resp404

        provider = ClsWireSearchProvider(enabled=True)
        result = provider.search("测试", max_results=3, days=7)
        self.assertFalse(result.success)
        self.assertEqual(result.results, [])

    def test_maps_into_search_service_providers(self) -> None:
        service = SearchService(
            cls_wire_enabled=True,
            searxng_public_instances_enabled=False,
        )
        self.assertTrue(any(p.name == "CLS" for p in service._providers))
        service_disabled = SearchService(
            cls_wire_enabled=False,
            searxng_public_instances_enabled=False,
        )
        self.assertFalse(any(p.name == "CLS" for p in service_disabled._providers))


if __name__ == "__main__":
    unittest.main()
