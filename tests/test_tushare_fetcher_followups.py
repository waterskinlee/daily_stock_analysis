# -*- coding: utf-8 -*-
"""Regression tests for post-merge Tushare follow-up fixes."""

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_fetcher import TushareFetcher


class TestTushareFetcherFollowUps(unittest.TestCase):
    """Cover rate limiting and cross-day trade-calendar refresh behavior."""

    @staticmethod
    def _make_fetcher() -> TushareFetcher:
        with patch.object(TushareFetcher, "_init_api", return_value=None):
            fetcher = TushareFetcher()
        fetcher._api = MagicMock()
        fetcher.priority = 2
        return fetcher

    def test_get_trade_time_refreshes_trade_calendar_when_day_changes(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.side_effect = [
            pd.DataFrame({"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}),
            pd.DataFrame({"cal_date": ["20260318", "20260317"], "is_open": [1, 1]}),
        ]

        with patch.object(
            fetcher,
            "_get_china_now",
            side_effect=[
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 18, 20, 0),
                datetime(2026, 3, 18, 20, 0),
            ],
        ), patch.object(fetcher, "_check_rate_limit") as rate_limit_mock:
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260317")
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260318")

        self.assertEqual(fetcher._api.trade_cal.call_count, 2)
        self.assertEqual(rate_limit_mock.call_count, 2)
    def test_get_trade_time_returns_latest_trade_date_on_non_trade_day(self) -> None:
        """Non-trade day (e.g. Saturday) should return the most recent trade
        date (Friday), not the one before it (Thursday).  Fixes #1009."""
        fetcher = self._make_fetcher()
        # 2026-03-21 is Saturday; Friday 20 and Thursday 19 are trade dates
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260314", "20260315", "20260316",
                             "20260317", "20260318", "20260319",
                             "20260320", "20260321"],
                "is_open": [0, 0, 1, 1, 1, 1, 1, 0],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # called twice: once by get_trade_time, once by _get_trade_dates
            side_effect=[datetime(2026, 3, 21, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Should be Friday (20th), NOT Thursday (19th)
        self.assertEqual(result, "20260320")

    def test_get_trade_time_trade_day_before_data_ready_returns_previous(self) -> None:
        """On a trade day within the early-late window, should return the
        previous trade date (data not ready yet for today)."""
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260319", "20260320"],
                "is_open": [1, 1],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # Friday 10:00 AM - within 00:00~19:00 window, data not ready
            side_effect=[datetime(2026, 3, 20, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Data not ready, should fall back to Thursday (19th)
        self.assertEqual(result, "20260319")
        
          
    def test_get_sector_rankings_rate_limits_calendar_and_rankings_api(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.moneyflow_ind_ths.return_value = pd.DataFrame(
            {
                "industry": ["AI", "消费"],
                "pct_change": [1.8, -0.6],
            }
        )

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 16, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            top, bottom = fetcher.get_sector_rankings(n=1)

        self.assertEqual(top, [{"name": "AI", "change_pct": 1.8}])
        self.assertEqual(bottom, [{"name": "消费", "change_pct": -0.6}])
        self.assertEqual(rate_limit_mock.call_count, 2)

    def test_get_chip_distribution_rate_limits_all_tushare_calls(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.cyq_chips.return_value = pd.DataFrame(
            {
                "price": [9.0, 10.0, 11.0],
                "percent": [20.0, 50.0, 30.0],
            }
        )
        fetcher._api.daily.return_value = pd.DataFrame({"close": [10.5]})

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 20, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            chip = fetcher.get_chip_distribution("600519")

        self.assertIsNotNone(chip)
        if chip is None:
            self.fail("expected chip distribution data")
        self.assertEqual(chip.date, "2026-03-17")
        self.assertAlmostEqual(chip.profit_ratio, 0.7)
        self.assertAlmostEqual(chip.avg_cost, 10.1)
        self.assertAlmostEqual(chip.concentration_90, 0.1)
        self.assertAlmostEqual(chip.concentration_70, 0.1)
        self.assertEqual(rate_limit_mock.call_count, 3)

    def test_convert_stock_code_accepts_exchange_prefixed_a_share(self) -> None:
        fetcher = self._make_fetcher()

        self.assertEqual(fetcher._convert_stock_code("SZ000001"), "000001.SZ")
        self.assertEqual(fetcher._convert_stock_code("SH600519"), "600519.SH")
        self.assertEqual(fetcher._convert_stock_code("605218"), "605218.SH")
        self.assertEqual(fetcher._convert_stock_code("600519.SS"), "600519.SH")

    # ---- get_capital_flow (moneyflow + moneyflow_ind_ths) ---------------------

    def test_get_capital_flow_populates_stock_and_sector(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.moneyflow.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH"] * 12,
                "trade_date": [
                    "20260807", "20260806", "20260805", "20260804", "20260803",
                    "20260731", "20260730", "20260729", "20260728", "20260727",
                    "20260724", "20260723",
                ],
                "net_mf_amount": [
                    -11454.78, 5000.0, 3000.0, 2000.0, 1000.0,
                    -500.0, 800.0, 1200.0, -900.0, 700.0,
                    400.0, -300.0,
                ],
            }
        )
        fetcher._api.moneyflow_ind_ths.return_value = pd.DataFrame(
            {
                "industry": ["医疗服务", "元件", "生物制品", "白酒", "煤炭"],
                "net_amount": [45.0, 13.0, 20.0, -8.0, -30.0],
            }
        )

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_capital_flow("600519", top_n=3)

        self.assertEqual(result["status"], "partial")
        # latest day net main inflow, 万元 -> 元
        self.assertAlmostEqual(result["stock_flow"]["main_net_inflow"], -11454.78 * 10000, places=2)
        # 5d / 10d sums
        self.assertAlmostEqual(result["stock_flow"]["inflow_5d"], (-11454.78 + 5000 + 3000 + 2000 + 1000) * 10000, places=2)
        self.assertAlmostEqual(
            result["stock_flow"]["inflow_10d"],
            (-11454.78 + 5000 + 3000 + 2000 + 1000 - 500 + 800 + 1200 - 900 + 700) * 10000,
            places=2,
        )
        top_names = [item["name"] for item in result["sector_rankings"]["top"]]
        bottom_names = [item["name"] for item in result["sector_rankings"]["bottom"]]
        self.assertEqual(top_names, ["医疗服务", "生物制品", "元件"])
        # bottom_n = top_n (3); with 5 industries the 3rd-largest also lands in bottom
        self.assertEqual(bottom_names, ["煤炭", "白酒", "元件"])
        self.assertIn("capital_stock:tushare_moneyflow", result["source_chain"])
        self.assertIn("capital_sector:tushare_moneyflow_ind_ths", result["source_chain"])
        self.assertEqual(result["errors"], [])

    def test_get_capital_flow_fail_open_on_empty_moneyflow(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.moneyflow.return_value = pd.DataFrame()

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_capital_flow("600519")

        self.assertEqual(result["status"], "not_supported")
        self.assertEqual(result["stock_flow"], {})
        self.assertTrue(any("moneyflow:empty" in e for e in result["errors"]))

    def test_get_capital_flow_fail_open_on_api_error(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.moneyflow.side_effect = Exception("quota")

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_capital_flow("600519")

        self.assertEqual(result["status"], "not_supported")
        self.assertTrue(any("moneyflow:Exception" in e for e in result["errors"]))
        # sector source still attempted
        self.assertEqual(result["sector_rankings"]["top"], [])
        self.assertEqual(result["sector_rankings"]["bottom"], [])

    # ---- get_shareholder_actions (pledge / repurchase / holder trades) --------

    def test_get_shareholder_actions_normalizes_risk_signals(self) -> None:
        fetcher = self._make_fetcher()
        today = datetime.now()
        recent = (today - timedelta(days=10)).strftime("%Y%m%d")
        older = (today - timedelta(days=500)).strftime("%Y%m%d")
        fetcher._api.pledge_stat.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "600519.SH"],
                "end_date": [recent, older],
                "pledge_count": [16, 8],
                "unrest_pledge": [103329.2, 50000.0],
                "rest_pledge": [0.0, 0.0],
                "total_share": [734180.51, 734180.51],
                "pledge_ratio": [34.07, 10.0],
            }
        )
        fetcher._api.repurchase.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "600519.SH"],
                "ann_date": [recent, older],
                "end_date": [None, older],
                "proc": ["实施", "完成"],
                "exp_date": [None, None],
                "vol": [17665380.0, 1000000.0],
                "amount": [999915944.13, 50000000.0],
                "high_limit": [65.0, 20.0],
                "low_limit": [50.14, 10.0],
            }
        )
        fetcher._api.stk_holdertrade.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "600519.SH", "600519.SH"],
                "ann_date": [recent, recent, older],
                "holder_name": ["大股东甲", "董事乙", "旧股东"],
                "holder_type": ["G", "G", "G"],
                "in_de": ["DE", "IN", "DE"],
                "change_vol": [3000000.0, 800000.0, 9999999.0],
                "change_ratio": [0.3, 0.08, 1.0],
                "after_share": [100000000.0, 2000000.0, 1.0],
                "after_ratio": [10.0, 0.2, 0.1],
                "avg_price": [40.0, 38.0, 10.0],
                "total_share": [100000000.0, 2000000.0, 1.0],
                "begin_date": [recent, recent, older],
                "close_date": [recent, recent, older],
            }
        )
        fetcher._api.stk_holdernumber.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "600519.SH"],
                "ann_date": [recent, older],
                "end_date": [recent, older],
                "holder_num": [100000, 120000],
            }
        )

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_shareholder_actions("600519", lookback_days=365, max_results=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["holder_concentration"]["holder_count"], 100000.0)
        self.assertEqual(result["holder_concentration"]["change_count"], -20000.0)
        self.assertEqual(result["holder_concentration"]["trend"], "concentrating")
        self.assertEqual(result["pledge"]["pledge_ratio"], 34.07)
        self.assertEqual(result["pledge"]["risk_level"], "medium")
        self.assertTrue(result["repurchase"]["has_active_plan"])
        self.assertEqual(result["repurchase"]["latest"]["status"], "实施")
        self.assertEqual(result["holder_trades"]["decrease_count"], 1)
        self.assertEqual(result["holder_trades"]["increase_count"], 1)
        self.assertEqual(result["holder_trades"]["net_change_volume"], -2200000.0)
        self.assertEqual(len(result["repurchase"]["recent_records"]), 1)
        self.assertEqual(len(result["holder_trades"]["recent_trades"]), 1)
        providers = [item["provider"] for item in result["source_chain"]]
        self.assertEqual(
            providers,
            ["tushare_pledge_stat", "tushare_repurchase", "tushare_stk_holdertrade", "tushare_stk_holdernumber"],
        )
        self.assertEqual(result["errors"], [])

    def test_get_shareholder_actions_fails_open_when_all_sources_error(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.pledge_stat.side_effect = RuntimeError("quota")
        fetcher._api.repurchase.side_effect = RuntimeError("quota")
        fetcher._api.stk_holdertrade.side_effect = RuntimeError("quota")
        fetcher._api.stk_holdernumber.side_effect = RuntimeError("quota")

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_shareholder_actions("600519")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["pledge"]["status"], "failed")
        self.assertEqual(result["repurchase"]["status"], "failed")
        self.assertEqual(result["holder_trades"]["status"], "failed")
        self.assertEqual(result["holder_concentration"]["status"], "failed")
        self.assertEqual(len(result["errors"]), 4)


    @patch.dict(sys.modules, {"tushare": MagicMock()})
    def test_legacy_realtime_quote_keeps_sz_hint_as_stock_symbol(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.quotation.side_effect = Exception("quota")

        tushare_module = sys.modules["tushare"]
        tushare_module.get_realtime_quotes.return_value = pd.DataFrame(
            [
                {
                    "name": "平安银行",
                    "price": "10.94",
                    "pre_close": "10.88",
                    "volume": "1000",
                    "amount": "2000",
                    "high": "11.00",
                    "low": "10.80",
                    "open": "10.90",
                }
            ]
        )

        quote = fetcher.get_realtime_quote("SZ000001")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "000001")
        self.assertEqual(quote.name, "平安银行")
        tushare_module.get_realtime_quotes.assert_called_once_with("000001")

    def test_realtime_quote_enriches_missing_fields_via_daily_basic(self) -> None:
        """quotation 缺 volume_ratio/turnover/pe/pb/mv -> daily_basic 补全，避免回退腾讯。"""
        fetcher = self._make_fetcher()
        fetcher._api.quotation.return_value = pd.DataFrame(
            {
                "ts_code": ["600916.SH"],
                "name": ["中国黄金"],
                "price": [7.77],
                "pct_chg": [0.52],
                "change": [0.04],
                "vol": [100000],
                "amount": [50000000],
                "high": [7.85],
                "low": [7.70],
                "open": [7.75],
                "pre_close": [7.73],
            }
        )
        fetcher._api.daily_basic.return_value = pd.DataFrame(
            {
                "ts_code": ["600916.SH"],
                "trade_date": ["20260807"],
                "turnover_rate": [2.8119],
                "volume_ratio": [1.06],
                "pe": [47.3928],
                "pb": [1.7877],
                "total_mv": [1305360],
                "circ_mv": [1305360],
            }
        )

        with patch.object(fetcher, "_check_rate_limit"):
            quote = fetcher.get_realtime_quote("600916")

        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote.volume_ratio, 1.06, places=4)
        self.assertAlmostEqual(quote.turnover_rate, 2.8119, places=4)
        self.assertAlmostEqual(quote.pe_ratio, 47.3928, places=4)
        self.assertAlmostEqual(quote.pb_ratio, 1.7877, places=4)
        self.assertAlmostEqual(quote.total_mv, 1305360, places=2)
        self.assertAlmostEqual(quote.circ_mv, 1305360, places=2)

    @patch.dict(sys.modules, {"tushare": MagicMock()})
    def test_legacy_realtime_quote_enriched_via_daily_basic(self) -> None:
        """xiaodefa 无 quotation（接口不存在）-> 旧版路径 + daily_basic 补全。

        这是 xiaodefa 实际走的路径：价格来自旧版实时接口，基础字段来自
        daily_basic，补全后 _quote_needs_supplement 为 False，无需回退腾讯。
        """
        fetcher = self._make_fetcher()
        fetcher._api.quotation.side_effect = Exception("接口不存在")

        tushare_module = sys.modules["tushare"]
        tushare_module.get_realtime_quotes.return_value = pd.DataFrame(
            [
                {
                    "name": "中国黄金",
                    "price": "7.77",
                    "pre_close": "7.73",
                    "volume": "1000000",
                    "amount": "50000000",
                    "high": "7.85",
                    "low": "7.70",
                    "open": "7.75",
                }
            ]
        )
        fetcher._api.daily_basic.return_value = pd.DataFrame(
            {
                "ts_code": ["600916.SH"],
                "trade_date": ["20260807"],
                "turnover_rate": [2.8119],
                "volume_ratio": [1.06],
                "pe": [47.3928],
                "pb": [1.7877],
                "total_mv": [1305360],
                "circ_mv": [1305360],
            }
        )

        with patch.object(fetcher, "_check_rate_limit"):
            quote = fetcher.get_realtime_quote("600916")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source.value, "tushare")
        self.assertAlmostEqual(quote.price, 7.77, places=2)
        self.assertAlmostEqual(quote.change_pct, 0.52, places=2)  # (7.77-7.73)/7.73*100
        self.assertAlmostEqual(quote.volume_ratio, 1.06, places=4)
        self.assertAlmostEqual(quote.turnover_rate, 2.8119, places=4)
        self.assertAlmostEqual(quote.pe_ratio, 47.3928, places=4)
        self.assertAlmostEqual(quote.pb_ratio, 1.7877, places=4)
        self.assertAlmostEqual(quote.total_mv, 1305360, places=2)
        self.assertAlmostEqual(quote.circ_mv, 1305360, places=2)
        # amplitude 由 (high-low)/pre_close 估算，不再为 None
        self.assertAlmostEqual(quote.amplitude, round((7.85 - 7.70) / 7.73 * 100, 2), places=2)

        # 补全后所有 SUPPLEMENT_FIELDS 均非 None，无需回退腾讯
        from data_provider.base import DataFetcherManager
        self.assertFalse(DataFetcherManager._quote_needs_supplement(quote))

    # ---- get_concept_rankings (dc_index 概念板块) ------------------------------

    @staticmethod
    def _concept_df():
        return pd.DataFrame(
            {
                "ts_code": ["885900.TI", "885901.TI", "885902.TI", "885903.TI", "885904.TI", "885905.TI"],
                "name": ["光刻胶", "CRO", "稀土", "白酒", "煤炭", "AI算力"],
                "idx_type": ["概念板块"] * 5 + ["行业板块"],
                "pct_change": [9.5, 8.2, 7.1, -3.4, -5.2, 6.0],
            }
        )

    def test_get_concept_rankings_filters_concept_and_ranks(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.dc_index.return_value = self._concept_df()
        TushareFetcher.clear_concept_rankings_cache_for_tests()

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_concept_rankings(3)

        self.assertIsNotNone(result)
        top, bottom = result
        self.assertEqual([item["name"] for item in top], ["光刻胶", "CRO", "稀土"])
        self.assertEqual([item["name"] for item in bottom], ["煤炭", "白酒", "稀土"])
        self.assertEqual(top[0]["change_pct"], 9.5)
        # 行业板块 (AI算力, idx_type != 概念板块) 被过滤
        self.assertNotIn("AI算力", [item["name"] for item in top] + [item["name"] for item in bottom])

    def test_get_concept_rankings_uses_latest_snapshot_and_unique_themes(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.dc_index.return_value = pd.DataFrame(
            {
                "ts_code": [
                    "BK0899.DC",
                    "BK0899.DC",
                    "BK0899.DC",
                    "BK1063.DC",
                    "BK0883.DC",
                    "BK9999.DC",
                ],
                "trade_date": [
                    "20260809",
                    "20260808",
                    "20260807",
                    "20260809",
                    "20260809",
                    "20260808",
                ],
                "name": ["CRO", "CRO", "CRO", "重组蛋白", "数字货币", "昨日异动"],
                "idx_type": ["概念板块"] * 6,
                "pct_change": [10.84, 10.84, 10.84, 7.9, -1.93, 99.0],
                "leading": ["百花医药", "百花医药", "百花医药", "百普赛斯", "芯原股份", "旧龙头"],
                "leading_code": [
                    "600721.SH",
                    "600721.SH",
                    "600721.SH",
                    "301080.SZ",
                    "688521.SH",
                    "000001.SZ",
                ],
            }
        )
        TushareFetcher.clear_concept_rankings_cache_for_tests()

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_concept_rankings(2)

        self.assertIsNotNone(result)
        top, bottom = result
        self.assertEqual([item["name"] for item in top], ["CRO", "重组蛋白"])
        self.assertEqual([item["name"] for item in bottom], ["数字货币", "重组蛋白"])
        self.assertEqual(top[0]["leading_code"], "600721.SH")
        self.assertEqual(len({item["name"] for item in top}), len(top))


    def test_get_concept_rankings_caches_by_n(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.dc_index.return_value = self._concept_df()
        TushareFetcher.clear_concept_rankings_cache_for_tests()

        with patch.object(fetcher, "_check_rate_limit") as rate_mock:
            r1 = fetcher.get_concept_rankings(3)
            r2 = fetcher.get_concept_rankings(3)
            r3 = fetcher.get_concept_rankings(5)

        self.assertEqual(rate_mock.call_count, 2)  # n=3 cached; n=5 refetches
        self.assertEqual(r1, r2)
        self.assertEqual(len(r3[0]), 5)

    def test_get_concept_rankings_fail_open_on_api_error(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.dc_index.side_effect = Exception("quota")
        TushareFetcher.clear_concept_rankings_cache_for_tests()

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_concept_rankings(3)

        self.assertIsNone(result)

    # ---- get_fundamental_bundle (growth/earnings/institution via tushare) -------

    @staticmethod
    def _fina_df():
        return pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "end_date": ["20260331"],
                "or_yoy": [6.538],
                "netprofit_yoy": [1.4714],
                "roe": [10.5687],
                "grossprofit_margin": [89.7592],
            }
        )

    def test_get_fundamental_bundle_populates_growth_and_earnings(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.fina_indicator.return_value = self._fina_df()
        fetcher._api.income.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "total_revenue": [54702912385.23], "n_income_attr_p": [27242512886.45]}
        )
        fetcher._api.cashflow.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "c_fr_sale_sg": [56392589148.92]}
        )
        fetcher._api.forecast.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "summary": ["业绩预增"]}
        )
        fetcher._api.express.return_value = pd.DataFrame()
        fetcher._api.dividend.return_value = pd.DataFrame()
        fetcher._api.top10_holders.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "hold_change": [0.5]}
        )

        with patch.object(fetcher, "_check_rate_limit"), patch(
            "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_institution_holdings_detail",
            return_value={},
        ):
            result = fetcher.get_fundamental_bundle("600519")

        self.assertEqual(result["status"], "partial")
        growth = result["growth"]
        self.assertAlmostEqual(growth["revenue_yoy"], 6.538, places=4)
        self.assertAlmostEqual(growth["net_profit_yoy"], 1.4714, places=4)
        self.assertAlmostEqual(growth["roe"], 10.5687, places=4)
        self.assertAlmostEqual(growth["gross_margin"], 89.7592, places=4)

        fr = result["earnings"]["financial_report"]
        self.assertEqual(fr["report_date"], "20260331")
        self.assertAlmostEqual(fr["revenue"], 54702912385.23, places=2)
        self.assertAlmostEqual(fr["net_profit_parent"], 27242512886.45, places=2)
        self.assertAlmostEqual(fr["operating_cash_flow"], 56392589148.92, places=2)

        self.assertEqual(result["earnings"]["forecast_summary"], "业绩预增")
        self.assertEqual(result["institution"]["top10_holder_change"], 0.5)
        self.assertIn("growth:tushare_fina_indicator", result["source_chain"])
        self.assertIn("earnings_forecast:tushare_forecast", result["source_chain"])
        self.assertIn("top10:tushare_top10_holders", result["source_chain"])
        self.assertEqual(result["errors"], [])

    def test_get_fundamental_bundle_fail_open_on_api_error(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.fina_indicator.side_effect = Exception("quota")
        fetcher._api.income.side_effect = Exception("quota")
        fetcher._api.cashflow.side_effect = Exception("quota")
        fetcher._api.forecast.side_effect = Exception("quota")
        fetcher._api.express.side_effect = Exception("quota")
        fetcher._api.dividend.side_effect = Exception("quota")
        fetcher._api.top10_holders.side_effect = Exception("quota")

        with patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_fundamental_bundle("600519")

        self.assertEqual(result["status"], "not_supported")
        self.assertEqual(result["growth"], {})
        self.assertEqual(result["earnings"], {})
        self.assertEqual(result["institution"], {})
        self.assertEqual(len(result["errors"]), 7)

    def test_get_fundamental_bundle_institution_fallback_via_akshare(self) -> None:
        """xiaodefa 无 top_inst_hold -> akshare stock_institute_hold 补 institution_holding_change。"""
        fetcher = self._make_fetcher()
        fetcher._api.fina_indicator.return_value = self._fina_df()
        fetcher._api.income.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "total_revenue": [1], "n_income_attr_p": [1]}
        )
        fetcher._api.cashflow.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "c_fr_sale_sg": [1]}
        )
        fetcher._api.forecast.return_value = pd.DataFrame()
        fetcher._api.express.return_value = pd.DataFrame()
        fetcher._api.dividend.return_value = pd.DataFrame()
        fetcher._api.top10_holders.return_value = pd.DataFrame()

        with patch.object(fetcher, "_check_rate_limit"), patch(
            "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_institution_holdings_detail",
            return_value={
                "institution_holding_change": 12.5,
                "institution_count": 352,
                "institution_holding_ratio": 2.42,
                "source": "akshare_stock_institute_hold",
            },
        ):
            result = fetcher.get_fundamental_bundle("600519")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["institution"]["institution_holding_change"], 12.5)
        self.assertEqual(result["institution"]["institution_count"], 352)
        self.assertEqual(result["institution"]["institution_holding_ratio"], 2.42)
        self.assertIn("institution:akshare_stock_institute_hold", result["source_chain"])
        # fail-open：akshare 异常不破坏 bundle
        self.assertEqual(result["errors"], [])

    def test_get_fundamental_bundle_institution_fallback_skipped_when_empty(self) -> None:
        """tushare 整体空时不再触发 akshare 机构补全（避免重复调用）。"""
        fetcher = self._make_fetcher()
        fetcher._api.fina_indicator.side_effect = Exception("quota")
        fetcher._api.income.side_effect = Exception("quota")
        fetcher._api.cashflow.side_effect = Exception("quota")
        fetcher._api.forecast.side_effect = Exception("quota")
        fetcher._api.express.side_effect = Exception("quota")
        fetcher._api.dividend.side_effect = Exception("quota")
        fetcher._api.top10_holders.side_effect = Exception("quota")

        with patch.object(fetcher, "_check_rate_limit"), patch(
            "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_institution_holdings_detail",
            return_value={"institution_holding_change": 99.0},
        ) as m:
            result = fetcher.get_fundamental_bundle("600519")

        m.assert_not_called()
        self.assertEqual(result["institution"], {})
        self.assertEqual(len(result["errors"]), 7)

    def test_get_fundamental_bundle_dividend_parses_tushare_rows(self) -> None:
        """dividend 返回非空 Series 时不再因 or 触发 Series 真值歧义 ValueError。"""
        fetcher = self._make_fetcher()
        fetcher._api.fina_indicator.return_value = self._fina_df()
        fetcher._api.income.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "total_revenue": [1], "n_income_attr_p": [1]}
        )
        fetcher._api.cashflow.return_value = pd.DataFrame(
            {"ts_code": ["600519.SH"], "end_date": ["20260331"], "c_fr_sale_sg": [1]}
        )
        fetcher._api.forecast.return_value = pd.DataFrame()
        fetcher._api.express.return_value = pd.DataFrame()
        fetcher._api.dividend.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "600519.SH"],
                "ex_date": ["20260710", "20250711"],
                "cash_div_tax": [28.02423, 27.993],
            }
        )
        fetcher._api.top10_holders.return_value = pd.DataFrame()

        with patch.object(fetcher, "_check_rate_limit"), patch(
            "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_institution_holdings_detail",
            return_value={},
        ):
            result = fetcher.get_fundamental_bundle("600519")

        self.assertEqual(result["status"], "partial")
        dividend = result["earnings"].get("dividend") or {}
        self.assertGreater(len(dividend.get("events", [])), 0)
        self.assertNotIn("dividend", result["errors"])

    # ---- 东财 zlsj 机构持仓 (RPT_MAIN_ORGHOLD) -------------------------------

    def test_get_institution_holdings_detail_prefers_zlsj(self) -> None:
        """zlsj 直连（RPT_MAIN_ORGHOLD）优先，返回机构数/持股比例/增减。"""
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter

        adapter = AkshareFundamentalAdapter()

        def _fake_datacenter(report_name, **_kwargs):
            if report_name == "RPT_MAIN_REPORTDATE":
                return [{"REPORT_DATE": "2026-06-30"}]
            if report_name == "RPT_MAIN_ORGHOLD":
                return [
                    {
                        "SECURITY_NAME_ABBR": "贵州茅台",
                        "ORG_TYPE": "00",
                        "HOULD_NUM": 988,
                        "FREESHARES_RATIO": 3.23121759,
                        "FREESHARES_RATIO_CHANGE": -1,
                        "HOLDCHA": "减仓",
                        "HOLDCHA_NUM": -25422467,
                    }
                ]
            if report_name == "RPT_HOLDERNUMLATEST":
                return [
                    {
                        "SECURITY_CODE": "600519",
                        "HOLDER_NUM": 243159,
                        "HOLDER_NUM_CHANGE": -12733,
                        "HOLDER_NUM_RATIO": -4.98,
                        "END_DATE": "2026-06-30",
                    }
                ]
            return []

        with patch.object(
            adapter, "_em_datacenter_get", side_effect=_fake_datacenter
        ), patch.object(adapter, "_call_df_candidates") as m:
            detail = adapter.get_institution_holdings_detail("600519")

        m.assert_not_called()  # zlsj 命中则不再走 akshare
        self.assertEqual(detail["institution_holding_change"], -25422467.0)
        self.assertEqual(detail["institution_count"], 988.0)
        self.assertAlmostEqual(detail["institution_holding_ratio"], 3.23121759, places=4)
        self.assertEqual(detail["hold_direction"], "减仓")
        self.assertEqual(detail["report_date"], "2026-06-30")
        self.assertEqual(detail["source"], "eastmoney_zlsj")
        # 股东户数（筹码集中度）
        self.assertEqual(detail["holder_num"], 243159.0)
        self.assertEqual(detail["holder_num_change"], -12733.0)
        self.assertAlmostEqual(detail["holder_num_ratio"], -4.98, places=2)

    def test_get_institution_holdings_detail_falls_back_to_akshare(self) -> None:
        """zlsj 失败/无数据时回退 akshare stock_institute_hold。"""
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter

        adapter = AkshareFundamentalAdapter()

        inst_df = pd.DataFrame(
            {
                "证券代码": ["600519"],
                "证券简称": ["贵州茅台"],
                "机构数变化": [10],
                "持股比例增幅": [1.5],
            }
        )

        with patch.object(adapter, "_em_datacenter_get", side_effect=Exception("datacenter down")), patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(inst_df, "stock_institute_hold", []),
        ):
            detail = adapter.get_institution_holdings_detail("600519")

        self.assertEqual(detail["source"], "akshare_stock_institute_hold")
        self.assertEqual(detail["institution_holding_change"], 10.0)
