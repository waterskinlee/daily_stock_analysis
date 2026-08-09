# -*- coding: utf-8 -*-
"""
Contract tests for get_capital_flow tool output semantics.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import _handle_get_capital_flow


class _DummyManagerOk:
    """Returns a well-formed capital flow context."""

    def get_capital_flow_context(self, _stock_code: str):
        return {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": 1500000.0,
                    "inflow_5d": 8000000.0,
                    "inflow_10d": 15000000.0,
                },
                "sector_rankings": {
                    "top": [{"name": "白酒", "inflow": 5e8}, {"name": "半导体", "inflow": 3e8}],
                    "bottom": [{"name": "煤炭", "inflow": -2e8}],
                },
                "block_trades": {
                    "status": "ok",
                    "latest_date": "2026-08-03",
                    "trade_count": 3,
                    "total_amount": 50000000.0,
                    "discount_trade_count": 1,
                    "premium_trade_count": 1,
                    "recent_trades": [{"trade_date": "2026-08-03", "deal_amount": 20000000.0}],
                },
                "margin_trading": {
                    "status": "ok",
                    "trade_date": "2026-08-07",
                    "financing_balance": 17544302364.0,
                    "financing_net_buy_amount": 17663935.0,
                    "financing_net_buy_5d": 131725962.0,
                },
                "popularity": {
                    "status": "ok",
                    "is_ranked": True,
                    "rank": 11,
                    "rank_change": -3,
                    "eastmoney_rank": 11,
                    "ths_rank": None,
                    "heat": None,
                    "concepts": ["白酒"],
                    "tag": "机构关注",
                    "is_top_20": True,
                    "primary_source": "eastmoney_hot_rank",
                    "top_stocks": [{"rank": 1, "code": "603259", "name": "药明康德"}],
                },
            },
            "errors": [],
        }


class _DummyManagerNotSupported:
    """Returns not_supported status (e.g. ETF or HK stock)."""

    def get_capital_flow_context(self, _stock_code: str):
        return {"status": "not_supported"}


class _DummyManagerRaises:
    """Simulates a fetch failure."""

    def get_capital_flow_context(self, _stock_code: str):
        raise RuntimeError("network timeout")


class TestGetCapitalFlowContract(unittest.TestCase):

    def test_ok_response_shape(self) -> None:
        """Happy path: key fields are present and values match the source data."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerOk(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["main_net_inflow"], 1500000.0)
        self.assertEqual(result["inflow_5d"], 8000000.0)
        self.assertEqual(result["inflow_10d"], 15000000.0)
        self.assertIn("sector_rankings", result)
        self.assertIn("top_inflow_sectors", result["sector_rankings"])
        self.assertIn("top_outflow_sectors", result["sector_rankings"])
        self.assertEqual(result["block_trades"]["trade_count"], 3)
        self.assertEqual(result["block_trades"]["total_amount"], 50000000.0)
        self.assertEqual(result["margin_trading"]["trade_date"], "2026-08-07")
        self.assertEqual(result["margin_trading"]["financing_net_buy_5d"], 131725962.0)
        self.assertEqual(result["popularity"]["rank"], 11)
        self.assertEqual(result["popularity"]["rank_change"], -3)
        self.assertEqual(result["popularity"]["concepts"], ["白酒"])
        self.assertTrue(result["popularity"]["is_top_20"])
        self.assertEqual(result["popularity"]["top_stocks"][0]["name"], "药明康德")
        # At most 3 items are returned per ranking list
        self.assertLessEqual(len(result["sector_rankings"]["top_inflow_sectors"]), 3)
        self.assertEqual(result["errors"], [])

    def test_not_supported_for_non_cn_or_etf(self) -> None:
        """ETF / non-CN stocks return status=not_supported with an explanatory note."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerNotSupported(),
        ):
            result = _handle_get_capital_flow("510300")

        self.assertEqual(result["stock_code"], "510300")
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("note", result)

    def test_exception_path_formatting(self) -> None:
        """Fetch errors are caught and returned with status=error."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerRaises(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "error")
        self.assertIn("capital flow fetch failed", result["error"])
        self.assertIn("network timeout", result["error"])


if __name__ == "__main__":
    unittest.main()
