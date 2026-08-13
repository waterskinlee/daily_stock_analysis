# -*- coding: utf-8 -*-
"""
===================================
Report Engine - Content integrity tests
===================================

Tests for check_content_integrity, apply_placeholder_fill, and retry/placeholder behavior.
"""

import json
import threading
import time
import sys
import unittest
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.analyzer import AnalysisResult, GeminiAnalyzer, check_content_integrity, apply_placeholder_fill


class TestCheckContentIntegrity(unittest.TestCase):
    """Content integrity check tests."""

    def test_pass_when_all_required_present(self) -> None:
        """Integrity passes when all mandatory fields are present."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110元"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_pass_when_signal_attribution_missing(self) -> None:
        """Signal attribution is optional and does not enter missing_fields."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110元"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_fail_when_analysis_summary_empty(self) -> None:
        """Integrity fails when analysis_summary is empty."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("analysis_summary", missing)

    def test_fail_when_one_sentence_missing(self) -> None:
        """Integrity fails when core_conclusion.one_sentence is missing."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.core_conclusion.one_sentence", missing)

    def test_fail_when_one_sentence_blank(self) -> None:
        """Integrity fails when one_sentence is blank whitespace."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "   "},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.core_conclusion.one_sentence", missing)

    def test_fail_when_stop_loss_missing_for_buy(self) -> None:
        """Integrity fails when stop_loss missing and decision_type is buy."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="买入",
            analysis_summary="稳健",
            decision_type="buy",
            dashboard={
                "core_conclusion": {"one_sentence": "可买入"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.battle_plan.sniper_points.stop_loss", missing)

    def test_pass_when_stop_loss_missing_for_sell(self) -> None:
        """Integrity passes when stop_loss missing and decision_type is sell."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看空",
            sentiment_score=35,
            operation_advice="卖出",
            analysis_summary="弱势",
            decision_type="sell",
            dashboard={
                "core_conclusion": {"one_sentence": "建议卖出"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_fail_when_risk_alerts_missing(self) -> None:
        """Integrity fails when intelligence.risk_alerts field is missing."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有"},
                "intelligence": {},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.intelligence.risk_alerts", missing)

    def test_phase_decision_missing_only_when_required(self) -> None:
        """Phase decision fields are required only for phase-aware analysis."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )

        ok, missing = check_content_integrity(result)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

        ok, missing = check_content_integrity(result, require_phase_decision=True)
        self.assertFalse(ok)
        self.assertIn("dashboard.phase_decision.phase_context", missing)
        self.assertIn("dashboard.phase_decision.watch_conditions", missing)
        self.assertIn("dashboard.phase_decision.data_limitations", missing)

    def test_fail_when_risk_alerts_is_none(self) -> None:
        """Integrity fails when risk_alerts is None."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有"},
                "intelligence": {"risk_alerts": None},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.intelligence.risk_alerts", missing)

    def test_fail_when_risk_alerts_is_invalid_type(self) -> None:
        """Integrity fails when risk_alerts is not list."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有"},
                "intelligence": {"risk_alerts": "需留意"},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.intelligence.risk_alerts", missing)

    def test_fail_when_stop_loss_is_blank(self) -> None:
        """Integrity fails when stop_loss is blank whitespace."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="买入",
            analysis_summary="稳健",
            decision_type="buy",
            dashboard={
                "core_conclusion": {"one_sentence": "可买入"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "   "}},
            },
        )
        ok, missing = check_content_integrity(result)
        self.assertFalse(ok)
        self.assertIn("dashboard.battle_plan.sniper_points.stop_loss", missing)


class TestApplyPlaceholderFill(unittest.TestCase):
    """Placeholder fill tests."""

    def test_fills_missing_analysis_summary(self) -> None:
        """Placeholder fills analysis_summary when missing."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="",
            decision_type="hold",
            dashboard={},
        )
        apply_placeholder_fill(result, ["analysis_summary"])
        self.assertEqual(result.analysis_summary, "待补充")

    def test_fills_missing_analysis_summary_in_english(self) -> None:
        """English report should use English placeholder text for missing analysis_summary."""
        result = AnalysisResult(
            code="600519",
            name="MacaoTech",
            report_language="en",
            trend_prediction="Bullish",
            sentiment_score=70,
            operation_advice="Buy",
            analysis_summary="",
            decision_type="buy",
            dashboard={},
        )
        apply_placeholder_fill(result, ["analysis_summary"])
        self.assertEqual(result.analysis_summary, "TBD")

    def test_fills_missing_stop_loss(self) -> None:
        """Placeholder fills stop_loss when missing."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="买入",
            analysis_summary="稳健",
            decision_type="buy",
            dashboard={"battle_plan": {"sniper_points": {}}},
        )
        apply_placeholder_fill(result, ["dashboard.battle_plan.sniper_points.stop_loss"])
        self.assertEqual(
            result.dashboard["battle_plan"]["sniper_points"]["stop_loss"],
            "待补充",
        )

    def test_fills_risk_alerts_empty_list(self) -> None:
        """Placeholder fills risk_alerts with empty list when missing."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={"intelligence": {}},
        )
        apply_placeholder_fill(result, ["dashboard.intelligence.risk_alerts"])
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], [])

    def test_fills_risk_alerts_when_none(self) -> None:
        """Placeholder fills risk_alerts when value is None."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            risk_warning="注意融资",
            dashboard={"intelligence": {"risk_alerts": None}},
        )
        apply_placeholder_fill(result, ["dashboard.intelligence.risk_alerts"])
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], ["注意融资"])

    def test_fills_risk_alerts_when_invalid_type(self) -> None:
        """Placeholder fills risk_alerts when value is non-list."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard={"intelligence": {"risk_alerts": "注意回撤"}},
        )
        apply_placeholder_fill(result, ["dashboard.intelligence.risk_alerts"])
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], [])

    def test_fills_risk_alerts_when_risk_warning_is_list(self) -> None:
        """Placeholder handles list risk_warning and flattens valid text values."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            risk_warning=["回撤风险", "波动加大"],
            dashboard={"intelligence": {"risk_alerts": ""}},
        )
        apply_placeholder_fill(result, ["dashboard.intelligence.risk_alerts"])
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], ["回撤风险", "波动加大"])

    def test_fills_risk_alerts_when_risk_warning_is_dict(self) -> None:
        """Placeholder serializes dict risk_warning into a string risk alert."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            risk_warning={"note": "技术面偏弱"},
            dashboard={"intelligence": {"risk_alerts": ""}},
        )
        apply_placeholder_fill(result, ["dashboard.intelligence.risk_alerts"])
        self.assertEqual(
            json.loads(result.dashboard["intelligence"]["risk_alerts"][0]),
            {"note": "技术面偏弱"},
        )

    def test_fills_stop_loss_when_blank(self) -> None:
        """Placeholder fills stop_loss when blank whitespace."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="买入",
            analysis_summary="稳健",
            decision_type="buy",
            dashboard={"battle_plan": {"sniper_points": {"stop_loss": "   "}}},
        )
        apply_placeholder_fill(result, ["dashboard.battle_plan.sniper_points.stop_loss"])
        self.assertEqual(
            result.dashboard["battle_plan"]["sniper_points"]["stop_loss"],
            "待补充",
        )

    def test_fills_stop_loss_when_invalid_type(self) -> None:
        """Placeholder fills stop_loss when value is invalid type."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="买入",
            analysis_summary="稳健",
            decision_type="buy",
            dashboard={"battle_plan": {"sniper_points": {"stop_loss": {}}}},
        )
        apply_placeholder_fill(result, ["dashboard.battle_plan.sniper_points.stop_loss"])
        self.assertEqual(
            result.dashboard["battle_plan"]["sniper_points"]["stop_loss"],
            "待补充",
        )

    def test_fills_none_dashboard_blocks_from_existing_context(self) -> None:
        """Placeholder fill handles null dashboard blocks and reuses existing result text."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="买入",
            analysis_summary="已有趋势摘要",
            risk_warning="跌破支撑需减仓",
            decision_type="buy",
            dashboard={
                "core_conclusion": None,
                "intelligence": None,
                "battle_plan": None,
            },
        )

        apply_placeholder_fill(
            result,
            [
                "dashboard.core_conclusion.one_sentence",
                "dashboard.intelligence.risk_alerts",
                "dashboard.battle_plan.sniper_points.stop_loss",
            ],
        )

        self.assertEqual(result.dashboard["core_conclusion"]["one_sentence"], "已有趋势摘要")
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], ["跌破支撑需减仓"])
        self.assertEqual(result.dashboard["battle_plan"]["sniper_points"]["stop_loss"], "待补充")

    def test_phase_decision_placeholder_fill_satisfies_integrity_contract(self) -> None:
        """Phase placeholders close the retry-exhausted integrity contract without fake conditions."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="震荡",
            sentiment_score=50,
            operation_advice="持有",
            analysis_summary="已有摘要",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "100"}},
                "phase_decision": {
                    "phase_context": "invalid",
                    "watch_conditions": "invalid",
                    "data_limitations": None,
                },
            },
        )

        ok, missing = check_content_integrity(result, require_phase_decision=True)
        self.assertFalse(ok)

        apply_placeholder_fill(result, missing)

        ok, missing = check_content_integrity(result, require_phase_decision=True)
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        phase_decision = result.dashboard["phase_decision"]
        self.assertEqual(phase_decision["phase_context"], {})
        self.assertEqual(phase_decision["watch_conditions"], [])
        self.assertEqual(phase_decision["data_limitations"], [])
        self.assertEqual(phase_decision["action_window"], "模型未提供阶段化行动窗口")
        self.assertEqual(phase_decision["immediate_action"], "模型未提供阶段化即时动作")
        self.assertEqual(phase_decision["next_check_time"], "模型未提供下一次检查点")
        self.assertEqual(phase_decision["confidence_reason"], "模型未提供阶段化置信度理由")

    def test_phase_context_fallback_restores_market_summary_subset(self) -> None:
        """Phase-context placeholder restores the authoritative market phase subset."""
        result = AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="震荡",
            sentiment_score=50,
            operation_advice="持有",
            analysis_summary="已有摘要",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "100"}},
                "phase_decision": {
                    "phase_context": "invalid",
                    "action_window": "盘中跟踪",
                    "immediate_action": "等待确认",
                    "watch_conditions": ["放量突破"],
                    "next_check_time": "14:50",
                    "confidence_reason": "数据完整",
                    "data_limitations": [],
                },
            },
        )
        market_phase_summary = {
            "phase": "intraday",
            "market": "cn",
            "market_local_time": "2026-08-12T10:43:44+08:00",
            "session_date": "2026-08-12",
            "effective_daily_bar_date": "2026-08-11",
            "is_trading_day": True,
            "is_market_open_now": True,
            "is_partial_bar": True,
            "minutes_to_open": None,
            "minutes_to_close": 256,
            "trigger_source": "api",
            "analysis_intent": "auto",
            "warnings": [],
        }

        ok, missing = check_content_integrity(result, require_phase_decision=True)
        self.assertFalse(ok)
        self.assertIn("dashboard.phase_decision.phase_context", missing)

        apply_placeholder_fill(
            result,
            missing,
            market_phase_summary=market_phase_summary,
        )

        phase_context = result.dashboard["phase_decision"]["phase_context"]
        self.assertIsInstance(phase_context, dict)
        self.assertEqual(phase_context["phase"], "intraday")
        self.assertEqual(phase_context["market"], "cn")
        self.assertEqual(phase_context["effective_daily_bar_date"], "2026-08-11")
        self.assertIs(phase_context["is_partial_bar"], True)
        self.assertEqual(phase_context["minutes_to_close"], 256)
        ok, missing = check_content_integrity(result, require_phase_decision=True)
        self.assertTrue(ok)
        self.assertEqual(missing, [])


class TestPreviousWatchVerificationIntegrity(unittest.TestCase):
    """Hard-constraint integrity checks for dashboard.previous_watch_verification."""

    def _base_result(self, **dashboard_overrides) -> AnalysisResult:
        dashboard = {
            "core_conclusion": {"one_sentence": "持有观望"},
            "intelligence": {"risk_alerts": []},
            "battle_plan": {"sniper_points": {"stop_loss": "100"}},
        }
        dashboard.update(dashboard_overrides)
        return AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="稳健",
            decision_type="hold",
            dashboard=dashboard,
        )

    def test_not_required_when_flag_off(self) -> None:
        """No pwv check when require_previous_watch_verification=False."""
        result = self._base_result()
        ok, missing = check_content_integrity(result)
        self.assertTrue(ok)
        self.assertNotIn("dashboard.previous_watch_verification", missing)

    def test_fail_when_pwv_missing(self) -> None:
        """Hard constraint fails when previous_watch_verification is absent."""
        result = self._base_result()
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn("dashboard.previous_watch_verification", missing)

    def test_fail_when_has_previous_not_bool(self) -> None:
        result = self._base_result(
            previous_watch_verification={"has_previous": "true", "items": [], "summary": "x"}
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn("dashboard.previous_watch_verification.has_previous", missing)

    def test_fail_when_has_previous_true_but_items_empty(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": True,
                "items": [],
                "summary": "整体结论",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn("dashboard.previous_watch_verification.items", missing)

    def test_fail_when_item_missing_fields(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": True,
                "items": [{"condition": "跌破 100 止损"}],
                "summary": "整体结论",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn(
            "dashboard.previous_watch_verification.items[0].status", missing
        )
        self.assertIn(
            "dashboard.previous_watch_verification.items[0].evidence", missing
        )
        self.assertIn(
            "dashboard.previous_watch_verification.items[0].impact", missing
        )

    def test_fail_when_status_invalid_enum(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": True,
                "items": [
                    {
                        "condition": "跌破 100 止损",
                        "status": "yes",
                        "evidence": "未跌破",
                        "impact": "继续持有",
                    }
                ],
                "summary": "整体结论",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn(
            "dashboard.previous_watch_verification.items[0].status", missing
        )

    def test_pass_when_pwv_complete(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": True,
                "previous_analysis_time": "2026-08-10 18:06",
                "items": [
                    {
                        "condition": "跌破 100 止损",
                        "status": "not_fulfilled",
                        "evidence": "今日最低 102",
                        "impact": "维持持有",
                    }
                ],
                "summary": "止损未触发",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_pass_when_no_previous_empty_state(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": False,
                "items": [],
                "summary": "无上次分析记录",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertTrue(ok)

    def test_fail_when_has_previous_false_but_items_nonempty(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": False,
                "items": [{"condition": "x", "status": "stale"}],
                "summary": "x",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn("dashboard.previous_watch_verification.items", missing)

    def test_fail_when_summary_blank(self) -> None:
        result = self._base_result(
            previous_watch_verification={
                "has_previous": False,
                "items": [],
                "summary": "",
            }
        )
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertFalse(ok)
        self.assertIn("dashboard.previous_watch_verification.summary", missing)

    def test_placeholder_fill_top_level_uses_previous_watch_context(self) -> None:
        """Fallback preserves real prior conditions and current market evidence."""
        result = self._base_result(
            phase_decision={
                "phase_context": {
                    "phase": "premarket",
                    "effective_daily_bar_date": "2026-08-11",
                },
                "data_limitations": ["盘前暂无当日分时行情。"],
            },
            data_perspective={"price_position": {"current_price": 1346.5}},
        )
        previous_context = {
            "analysis_time": "2026-08-11 09:30",
            "watch_conditions": ["放量站上 1363.35", "跌破 1312.14 止损"],
        }

        apply_placeholder_fill(
            result,
            ["dashboard.previous_watch_verification"],
            previous_watch_context=previous_context,
        )

        pwv = result.dashboard["previous_watch_verification"]
        self.assertTrue(pwv["has_previous"])
        self.assertEqual(pwv["previous_analysis_time"], "2026-08-11 09:30")
        self.assertEqual(
            [item["condition"] for item in pwv["items"]],
            previous_context["watch_conditions"],
        )
        self.assertTrue(all(item["status"] == "partially_fulfilled" for item in pwv["items"]))
        self.assertTrue(all("1346.5" in item["evidence"] for item in pwv["items"]))
        self.assertTrue(all(item["impact"] for item in pwv["items"]))
        self.assertEqual(pwv["verification_source"], "deterministic_fallback")
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_placeholder_fill_has_previous_false_state_passes_recheck(self) -> None:
        """The no-previous empty-state placeholder should pass re-check."""
        result = self._base_result()
        apply_placeholder_fill(result, ["dashboard.previous_watch_verification"])
        # Force has_previous=False to simulate the no-previous scenario.
        result.dashboard["previous_watch_verification"]["has_previous"] = False
        ok, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])


class TestIntegrityRetryPrompt(unittest.TestCase):
    """Retry prompt construction tests."""

    def test_retry_prompt_includes_previous_response(self) -> None:
        """Retry prompt should carry previous response so补全是增量的。"""
        with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
            analyzer = GeminiAnalyzer()
        prompt = analyzer._build_integrity_retry_prompt(
            "原始提示",
            '{"analysis_summary": "已有内容"}',
            ["dashboard.core_conclusion.one_sentence"],
        )
        self.assertIn("原始提示", prompt)
        self.assertIn('{"analysis_summary": "已有内容"}', prompt)
        self.assertIn("dashboard.core_conclusion.one_sentence", prompt)


class TestAgentIntegrityRepair(unittest.TestCase):
    """Agent-path bounded LLM repair before placeholder fill."""

    def _make_pipeline(self):
        from src.core.pipeline import StockAnalysisPipeline

        pipe = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipe.config = type("C", (), {"report_language": "zh"})()
        return pipe
    def _base_result(self, sentiment_score=70, operation_advice="持有", analysis_summary="稳健", **dashboard_overrides):
        dashboard = {
            "core_conclusion": {"one_sentence": "持有"},
            "intelligence": {"risk_alerts": ["风险1"]},
            "battle_plan": {"sniper_points": {"stop_loss": "100"}},
            "previous_watch_verification": {
                "has_previous": True,
                "items": [
                    {
                        "condition": "跌破100止损",
                        "status": "not_fulfilled",
                        "evidence": "未跌破",
                        "impact": "继续持有",
                    }
                ],
                "summary": "止损未触发",
            },
        }
        dashboard.update(dashboard_overrides)
        return AnalysisResult(
            code="600519",
            name="贵州茅台",
            trend_prediction="看多",
            sentiment_score=sentiment_score,
            operation_advice=operation_advice,
            analysis_summary=analysis_summary,
            decision_type="hold",
            dashboard=dashboard,
        )

    def _make_context(self, **overrides):
        ctx = {
            "previous_analysis_context": "## 上次分析观察点\n- 跌破100止损",
            "realtime_quote": {"price": 105.2, "change_pct": 2.3, "volume_ratio": 1.8},
            "trend_result": {
                "trend_status": "bullish",
                "buy_signal": "buy",
                "signal_score": 65,
                "ma5": 100.0,
                "ma10": 99.0,
                "ma20": 98.0,
                "risk_factors": ["高位放量"],
            },
        }
        ctx.update(overrides)
        return ctx

    def test_no_adapter_returns_false(self) -> None:
        """Repair returns False (→ placeholder) when no LLM adapter available."""
        pipe = self._make_pipeline()
        pipe._resolve_repair_llm_adapter = lambda: None
        result = self._base_result(sentiment_score=None)
        ok, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertFalse(repaired)
        self.assertEqual(remaining, ["sentiment_score"])

    def test_empty_response_returns_false(self) -> None:
        """Empty/error LLM response → False."""
        pipe = self._make_pipeline()

        class _Adapter:
            def call_text(self, messages, **kw):
                return type("R", (), {"content": "", "provider": "error"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(sentiment_score=None)
        ok, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertFalse(repaired)
        self.assertEqual(remaining, ["sentiment_score"])

    def test_timeout_retries_within_attempt_budget(self) -> None:
        """Timeouts are transient and retry only within the attempt budget."""
        pipe = self._make_pipeline()
        calls = [0]

        class _Adapter:
            def call_text(self, messages, **kw):
                calls[0] += 1
                raise TimeoutError("60s")

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(sentiment_score=None)
        _, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=2,
            require_phase_decision=False,
        )
        self.assertFalse(repaired)
        self.assertEqual(remaining, ["sentiment_score"])
        self.assertEqual(calls[0], 2)

    def test_parse_error_is_not_retried(self) -> None:
        """Deterministic JSON parse errors do not consume another attempt."""
        pipe = self._make_pipeline()
        calls = [0]

        class _Adapter:
            def call_text(self, messages, **kw):
                calls[0] += 1
                return type("R", (), {"content": "bad json", "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(sentiment_score=None)
        _, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=2,
            require_phase_decision=False,
        )
        self.assertFalse(repaired)
        self.assertEqual(remaining, ["sentiment_score"])
        self.assertEqual(calls[0], 1)

    def test_provider_error_retries_then_succeeds(self) -> None:
        """Transient provider errors retry within the bounded attempt budget."""
        pipe = self._make_pipeline()
        calls = [0]

        class _Adapter:
            def call_text(self, messages, **kw):
                calls[0] += 1
                if calls[0] == 1:
                    return type("R", (), {"content": "temporary", "provider": "error"})()
                return type("R", (), {"content": '{"analysis_summary":"已补齐"}', "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(analysis_summary="")
        _, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=2,
            require_phase_decision=False,
        )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        self.assertEqual(calls[0], 2)
        self.assertEqual(result.analysis_summary, "已补齐")

    def test_successful_repair_merges_and_passes_recheck(self) -> None:
        """Valid repair response fills missing fields → re-check passes."""
        pipe = self._make_pipeline()
        repair = {
            "sentiment_score": 82,
            "operation_advice": "加仓",
            "analysis_summary": "新摘要",
            "dashboard": {
                "intelligence": {"risk_alerts": ["新风险"]},
                "battle_plan": {"sniper_points": {"stop_loss": 95.5}},
            },
        }

        class _Adapter:
            def call_text(self, messages, **kw):
                return type("R", (), {"content": json.dumps(repair, ensure_ascii=False), "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = AnalysisResult(
            code="600519",
            name="X",
            trend_prediction="看多",
            sentiment_score=None,
            operation_advice="",
            analysis_summary="",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "x"},
                "previous_watch_verification": {
                    "has_previous": True,
                    "items": [
                        {"condition": "c", "status": "fulfilled", "evidence": "e", "impact": "i"}
                    ],
                    "summary": "s",
                },
            },
        )
        ok, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        self.assertEqual(result.sentiment_score, 82)
        self.assertEqual(result.operation_advice, "加仓")
        self.assertEqual(result.dashboard["battle_plan"]["sniper_points"]["stop_loss"], 95.5)

    def test_merge_only_does_not_overwrite_existing(self) -> None:
        """Repair response trying to overwrite existing fields is ignored."""
        pipe = self._make_pipeline()
        result = self._base_result(sentiment_score=75)
        result.analysis_summary = ""
        ok, missing = check_content_integrity(result)
        # Only analysis_summary is missing; repair tries to overwrite everything.
        repair = {
            "sentiment_score": 999,
            "operation_advice": "卖出",
            "analysis_summary": "新摘要",
            "dashboard": {
                "core_conclusion": {"one_sentence": "覆盖"},
                "intelligence": {"risk_alerts": ["覆盖"]},
                "battle_plan": {"sniper_points": {"stop_loss": 999}},
            },
        }

        class _Adapter:
            def call_text(self, messages, **kw):
                return type("R", (), {"content": json.dumps(repair, ensure_ascii=False), "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        # Existing fields preserved.
        self.assertEqual(result.sentiment_score, 75)
        self.assertEqual(result.operation_advice, "持有")
        self.assertEqual(result.dashboard["core_conclusion"]["one_sentence"], "持有")
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], ["风险1"])
        self.assertEqual(result.dashboard["battle_plan"]["sniper_points"]["stop_loss"], "100")
        # Only analysis_summary was filled.
        self.assertEqual(result.analysis_summary, "新摘要")

    def test_bare_shape_repair_root_normalization(self) -> None:
        """Bare dashboard fragments repair structure but not evidence fields."""
        pipe = self._make_pipeline()
        result = AnalysisResult(
            code="600519",
            name="X",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="S",
            decision_type="hold",
            dashboard={
                "core_conclusion": {"one_sentence": "x"},
                "previous_watch_verification": None,
            },
        )
        bare = {
            "intelligence": {"risk_alerts": ["新风险"]},
            "battle_plan": {"sniper_points": {"stop_loss": 95.5}},
        }

        class _Adapter:
            def call_text(self, messages, **kw):
                return type("R", (), {"content": json.dumps(bare, ensure_ascii=False), "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        _, missing = check_content_integrity(
            result, require_previous_watch_verification=True
        )
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        self.assertEqual(result.dashboard["intelligence"]["risk_alerts"], ["新风险"])
        self.assertEqual(result.dashboard["battle_plan"]["sniper_points"]["stop_loss"], 95.5)
        self.assertIsNone(result.dashboard["previous_watch_verification"])

    def test_prompt_includes_previous_section_and_digest(self) -> None:
        """Repair prompt carries the previous watch section + data digest."""
        pipe = self._make_pipeline()
        messages = pipe._build_agent_integrity_repair_prompt(
            initial_context=self._make_context(),
            trend_result=None,
            realtime_quote=None,
            missing_fields=["dashboard.phase_decision.data_limitations"],
            report_language="zh",
        )
        self.assertEqual(len(messages), 2)
        usr = messages[1]["content"]
        self.assertIn("上次分析观察点", usr)
        self.assertIn("跌破100止损", usr)
        self.assertIn("当前价", usr)
        self.assertIn("105.2", usr)
        self.assertIn("bullish", usr)
        self.assertIn("MA5", usr)

    def test_prompt_en_locale(self) -> None:
        """English repair prompt uses English labels."""
        pipe = self._make_pipeline()
        messages = pipe._build_agent_integrity_repair_prompt(
            initial_context=self._make_context(),
            trend_result=None,
            realtime_quote=None,
            missing_fields=["dashboard.phase_decision.data_limitations"],
            report_language="en",
        )
        usr = messages[1]["content"]
        self.assertIn("Previous watch-point context", usr)
        self.assertIn("Current price", usr)

    def test_prompt_empty_previous_section_shows_placeholder(self) -> None:
        """Empty previous section renders '(无)' / '(none)'."""
        pipe = self._make_pipeline()
        ctx = self._make_context(previous_analysis_context="")
        messages = pipe._build_agent_integrity_repair_prompt(
            initial_context=ctx,
            trend_result=None,
            realtime_quote=None,
            missing_fields=["sentiment_score"],
            report_language="zh",
        )
        self.assertIn("(无)", messages[1]["content"])

    def test_parse_repair_dashboard_fenced_with_prose(self) -> None:
        """Fenced JSON with leading/trailing prose is extracted."""
        pipe = self._make_pipeline()
        content = 'Here is the JSON:\n```json\n{"a": 1}\n```\nDone.'
        result = pipe._parse_repair_dashboard(content)
        self.assertEqual(result, {"a": 1})

    def test_parse_repair_dashboard_bare_with_prose(self) -> None:
        """Bare JSON with leading prose is extracted."""
        pipe = self._make_pipeline()
        content = 'Result: {"a": 1, "b": [2]}'
        result = pipe._parse_repair_dashboard(content)
        self.assertEqual(result, {"a": 1, "b": [2]})

    def test_parse_repair_dashboard_uses_first_object(self) -> None:
        """The first complete JSON object is accepted despite trailing prose."""
        pipe = self._make_pipeline()
        result = pipe._parse_repair_dashboard('{"a":1} and {"b":2}')
        self.assertEqual(result, {"a": 1})

    def test_parse_repair_dashboard_empty(self) -> None:
        """Empty content → None."""
        pipe = self._make_pipeline()
        self.assertIsNone(pipe._parse_repair_dashboard(""))

    def test_parse_repair_dashboard_non_dict(self) -> None:
        """Non-dict JSON (array) → None."""
        pipe = self._make_pipeline()
        self.assertIsNone(pipe._parse_repair_dashboard("[1, 2, 3]"))

    def test_merge_items_index_path(self) -> None:
        """items[N].field path is merged correctly."""
        pipe = self._make_pipeline()
        result = AnalysisResult(
            code="600519",
            name="X",
            trend_prediction="看多",
            sentiment_score=70,
            operation_advice="持有",
            analysis_summary="S",
            decision_type="hold",
            dashboard={
                "previous_watch_verification": {
                    "has_previous": True,
                    "items": [
                        {"condition": "c1", "status": None, "evidence": None, "impact": None}
                    ],
                    "summary": "s",
                }
            },
        )
        repair = {
            "previous_watch_verification": {
                "items": [
                    {"status": "not_fulfilled", "evidence": "ev", "impact": "im"}
                ]
            }
        }
        missing = [
            "dashboard.previous_watch_verification.items[0].status",
            "dashboard.previous_watch_verification.items[0].evidence",
            "dashboard.previous_watch_verification.items[0].impact",
        ]
        merged = pipe._merge_repaired_fields(
            result, repaired_dashboard=repair, missing_fields=missing
        )
        self.assertEqual(merged, 3)
        item = result.dashboard["previous_watch_verification"]["items"][0]
        self.assertEqual(item["status"], "not_fulfilled")
        self.assertEqual(item["evidence"], "ev")
        self.assertEqual(item["impact"], "im")
        self.assertEqual(item["condition"], "c1")  # untouched


    def test_repair_uses_delta_contract_and_disables_reasoning(self) -> None:
        """Repair sends only the missing fragment with bounded generation."""
        pipe = self._make_pipeline()
        calls = []
        repair = {"analysis_summary": "补全摘要"}

        class _Adapter:
            def call_text(self, messages, **kwargs):
                calls.append((messages, kwargs))
                return type("R", (), {"content": json.dumps(repair), "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(analysis_summary="")
        ok, missing = check_content_integrity(result)
        self.assertIn("analysis_summary", missing)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        self.assertEqual(result.analysis_summary, "补全摘要")
        self.assertEqual(len(calls), 1)
        messages, kwargs = calls[0]
        self.assertEqual(kwargs["temperature"], 0)
        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertLessEqual(kwargs["timeout"], pipe._REPAIR_CALL_TIMEOUT_S)
        self.assertNotIn("当前仪表盘", messages[1]["content"])
        self.assertNotIn("Current dashboard", messages[1]["content"])

    def test_previous_watch_fields_are_excluded_from_repair(self) -> None:
        """Evidence/system-derived fields bypass the LLM repair."""
        from src.core.pipeline import StockAnalysisPipeline

        repairable, evidence = StockAnalysisPipeline._partition_integrity_missing_fields(
            [
                "dashboard.phase_decision.data_limitations",
                "dashboard.phase_decision.phase_context",
                "dashboard.previous_watch_verification",
                "dashboard.previous_watch_verification.items[0].evidence",
            ]
        )
        self.assertEqual(repairable, ["dashboard.phase_decision.data_limitations"])
        self.assertEqual(
            evidence,
            [
                "dashboard.phase_decision.phase_context",
                "dashboard.previous_watch_verification",
                "dashboard.previous_watch_verification.items[0].evidence",
            ],
        )

    def test_partial_repair_returns_remaining_without_losing_filled_score(self) -> None:
        """A partial repair must not let placeholder fill erase real values."""
        pipe = self._make_pipeline()

        class _Adapter:
            def call_text(self, messages, **kwargs):
                return type("R", (), {"content": json.dumps({"sentiment_score": 82}), "provider": "ok"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(sentiment_score=None, analysis_summary="")
        _, missing = check_content_integrity(result)
        repaired, remaining = pipe._attempt_integrity_repair(
            result,
            missing_fields=missing,
            initial_context=self._make_context(),
            trend_result=None,
            report_language="zh",
            max_retries=1,
            require_phase_decision=False,
        )
        self.assertFalse(repaired)
        self.assertIn("analysis_summary", remaining)
        self.assertNotIn("sentiment_score", remaining)
        apply_placeholder_fill(result, remaining)
        self.assertEqual(result.sentiment_score, 82)

    def test_repair_deadline_returns_without_waiting_for_adapter(self) -> None:
        """A blocked adapter cannot hold the analysis past the repair deadline."""
        pipe = self._make_pipeline()
        started = threading.Event()
        release = threading.Event()

        class _Adapter:
            def call_text(self, messages, **kwargs):
                started.set()
                release.wait(2)
                return type("R", (), {"content": "{}", "provider": "ok"})()

        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            pipe._call_repair_llm_with_deadline(
                _Adapter(), [], timeout=0.03
            )
        elapsed = time.monotonic() - began
        release.set()
        self.assertTrue(started.is_set())
        self.assertLess(elapsed, 0.5)

    def test_invalid_repair_values_are_rejected_before_merge(self) -> None:
        """Repair candidates with invalid types or ranges never pollute result."""
        pipe = self._make_pipeline()
        result = self._base_result(
            phase_decision={
                "action_window": "盘中",
                "immediate_action": "观察",
                "watch_conditions": [],
                "next_check_time": "10:00",
                "confidence_reason": "数据有限",
                "data_limitations": [],
            }
        )
        result.sentiment_score = None
        merged = pipe._merge_repaired_fields(
            result,
            repaired_dashboard={
                "sentiment_score": 101,
                "dashboard": {"phase_decision": {"phase_context": []}},
            },
            missing_fields=[
                "sentiment_score",
                "dashboard.phase_decision.phase_context",
            ],
        )
        self.assertEqual(merged, 0)
        self.assertIsNone(result.sentiment_score)
        self.assertNotIn("phase_context", result.dashboard["phase_decision"])

    def test_repair_records_start_and_result_diagnostics(self) -> None:
        """Every repair completion is visible in run diagnostics."""
        pipe = self._make_pipeline()

        class _Adapter:
            def call_text(self, messages, **kwargs):
                return type("R", (), {"content": '{"analysis_summary":"已补齐"}', "provider": "newapio"})()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(analysis_summary="")
        _, missing = check_content_integrity(result)
        with patch("src.core.pipeline.record_llm_run_started") as started, patch(
            "src.core.pipeline.record_llm_run"
        ) as recorded:
            repaired, remaining = pipe._attempt_integrity_repair(
                result,
                missing_fields=missing,
                initial_context=self._make_context(),
                trend_result=None,
                report_language="zh",
                max_retries=1,
                require_phase_decision=False,
            )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        started.assert_called_once()
        recorded.assert_called_once()
        self.assertTrue(recorded.call_args.kwargs["success"])
        self.assertEqual(recorded.call_args.kwargs["call_type"], "integrity_repair")

    def test_repair_records_token_and_model_observability(self) -> None:
        """Repair completion carries prompt/completion tokens and models_tried."""
        pipe = self._make_pipeline()

        class _Adapter:
            def call_text(self, messages, **kwargs):
                return type(
                    "R",
                    (),
                    {
                        "content": '{"analysis_summary":"已补齐"}',
                        "provider": "newapia",
                        "model": "anthropic/deepseek-v4-flash",
                        "usage": {
                            "prompt_tokens": 512,
                            "completion_tokens": 64,
                            "total_tokens": 576,
                        },
                        "models_tried": ["anthropic/deepseek-v4-flash"],
                    },
                )()

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(analysis_summary="")
        _, missing = check_content_integrity(result)
        with patch("src.core.pipeline.record_llm_run") as recorded:
            repaired, _ = pipe._attempt_integrity_repair(
                result,
                missing_fields=missing,
                initial_context=self._make_context(),
                trend_result=None,
                report_language="zh",
                max_retries=1,
                require_phase_decision=False,
            )
        self.assertTrue(repaired)
        kwargs = recorded.call_args.kwargs
        self.assertEqual(kwargs["prompt_tokens"], 512)
        self.assertEqual(kwargs["completion_tokens"], 64)
        self.assertEqual(kwargs["tokens"], 576)
        self.assertEqual(kwargs["models_tried"], ["anthropic/deepseek-v4-flash"])
        self.assertEqual(kwargs["model"], "anthropic/deepseek-v4-flash")

    def test_phase_context_never_counts_as_repair_failure(self) -> None:
        """phase_context is deterministic system data: the repair LLM is not
        asked to regenerate it. When it is the only missing structural field,
        the repair short-circuits as complete and never records a failure.
        """
        pipe = self._make_pipeline()

        class _Adapter:
            def call_text(self, messages, **kwargs):
                raise AssertionError("repair LLM must not be called")

        pipe._resolve_repair_llm_adapter = lambda: _Adapter()
        result = self._base_result(
            phase_decision={
                "action_window": "盘中跟踪",
                "immediate_action": "等待确认",
                "watch_conditions": ["放量突破"],
                "next_check_time": "14:50",
                "confidence_reason": "数据完整",
                "data_limitations": [],
            }
        )
        ok, missing = check_content_integrity(result, require_phase_decision=True)
        self.assertFalse(ok)
        self.assertIn("dashboard.phase_decision.phase_context", missing)
        self.assertEqual(
            missing, ["dashboard.phase_decision.phase_context"]
        )

        with patch("src.core.pipeline.record_llm_run") as recorded:
            repaired, remaining = pipe._attempt_integrity_repair(
                result,
                missing_fields=missing,
                initial_context=self._make_context(),
                trend_result=None,
                report_language="zh",
                max_retries=1,
                require_phase_decision=True,
            )
        self.assertTrue(repaired)
        self.assertEqual(remaining, [])
        recorded.assert_not_called()