# -*- coding: utf-8 -*-
"""Tests for previous-analysis watch-point reuse (soft constraint).

Covers:
- ``load_previous_analysis_context`` selection/filter rules.
- ``format_previous_analysis_section`` rendering (zh/en, caps).
- Prompt integration: the legacy ``_format_prompt`` and the Agent
  ``_build_user_message`` both render the injected section.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.analysis_previous_context import (
    format_previous_analysis_section,
    load_previous_analysis_context,
)


def _record(**overrides):
    base = {
        "id": 1,
        "query_id": "q1",
        "code": "600519",
        "name": "贵州茅台",
        "report_type": "stock_analysis",
        "operation_advice": "持有观察",
        "analysis_summary": "放量突破 MA20，MACD 金叉，但主力资金未确认流入，先按观察处理。",
        "raw_result": None,
        "ideal_buy": 1500.0,
        "secondary_buy": 1480.0,
        "stop_loss": 1450.0,
        "take_profit": 1620.0,
        "created_at": datetime.now() - timedelta(days=1),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _raw_with_watch(watch_conditions, **dashboard_overrides):
    dashboard = {
        "phase_decision": {
            "watch_conditions": watch_conditions,
            "next_check_time": "2026-08-11 09:30",
        },
        **dashboard_overrides,
    }
    return json.dumps({"dashboard": dashboard}, ensure_ascii=False)


class TestLoadPreviousAnalysisContext:
    def test_picks_most_recent_eligible_record(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        old = _record(
            id=1,
            created_at=now - timedelta(days=2),
            operation_advice="买入",
            raw_result=_raw_with_watch(["突破 1620 则加仓"]),
        )
        recent = _record(
            id=2,
            created_at=now - timedelta(days=1),
            operation_advice="持有观察",
            raw_result=_raw_with_watch(["跌破 1450 止损离场"]),
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [recent, old])
        prev = load_previous_analysis_context(db, "600519", now=now)
        assert prev is not None
        assert prev["operation_advice"] == "持有观察"
        assert prev["watch_conditions"] == ["跌破 1450 止损离场"]

    def test_same_day_earlier_record_is_eligible(self):
        now = datetime(2026, 8, 10, 15, 0, 0)
        premarket = _record(
            id=1,
            created_at=datetime(2026, 8, 10, 8, 30, 0),
            raw_result=_raw_with_watch(["观察量能是否放大"]),
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [premarket])
        prev = load_previous_analysis_context(db, "600519", now=now)
        assert prev is not None
        assert prev["watch_conditions"] == ["观察量能是否放大"]

    def test_skips_future_and_same_instant_records(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        future = _record(
            id=1,
            created_at=now + timedelta(hours=1),
            raw_result=_raw_with_watch(["未来条件"]),
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [future])
        assert load_previous_analysis_context(db, "600519", now=now) is None

    def test_skips_market_review(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        review = _record(
            id=1,
            report_type="market_review",
            created_at=now - timedelta(days=1),
            raw_result=_raw_with_watch(["大盘观察"]),
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [review])
        assert load_previous_analysis_context(db, "600519", now=now) is None

    def test_respects_exclude_query_id(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        current = _record(
            id=2,
            query_id="current-run",
            created_at=now - timedelta(hours=2),
            raw_result=_raw_with_watch(["本次条件"]),
        )
        older = _record(
            id=1,
            query_id="old-run",
            created_at=now - timedelta(days=1),
            raw_result=_raw_with_watch(["上次条件"]),
        )
        captured = {}

        def fake_get(**kwargs):
            captured.update(kwargs)
            # Simulate the DB layer honoring exclude_query_id.
            return [record for record in (current, older) if record.query_id != kwargs.get("exclude_query_id")]

        db = SimpleNamespace(get_analysis_history=fake_get)
        prev = load_previous_analysis_context(
            db, "600519", exclude_query_id="current-run", now=now
        )
        assert captured.get("exclude_query_id") == "current-run"
        assert prev["watch_conditions"] == ["上次条件"]

    def test_falls_back_to_battle_plan_checklist(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        record = _record(
            id=1,
            created_at=now - timedelta(days=1),
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {"action_checklist": ["回踩 1500 企稳再入场"]}
                    }
                },
                ensure_ascii=False,
            ),
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [record])
        prev = load_previous_analysis_context(db, "600519", now=now)
        assert prev["watch_conditions"] == ["回踩 1500 企稳再入场"]

    def test_returns_none_when_record_has_no_reusable_content(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        blank = _record(
            id=1,
            created_at=now - timedelta(days=1),
            raw_result=None,
            operation_advice=None,
            analysis_summary=None,
            ideal_buy=None,
            secondary_buy=None,
            stop_loss=None,
            take_profit=None,
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [blank])
        assert load_previous_analysis_context(db, "600519", now=now) is None

    def test_extracts_sniper_points_and_truncates_summary(self):
        now = datetime(2026, 8, 10, 12, 0, 0)
        record = _record(
            id=1,
            created_at=now - timedelta(days=1),
            analysis_summary="长" * 300,
            raw_result=None,
            operation_advice=None,
        )
        db = SimpleNamespace(get_analysis_history=lambda **kw: [record])
        prev = load_previous_analysis_context(db, "600519", now=now)
        assert prev["stop_loss"] == 1450.0
        assert prev["ideal_buy"] == 1500.0
        assert len(prev["analysis_summary"]) == 121  # 120 + ellipsis
        assert prev["analysis_summary"].endswith("…")

    def test_returns_none_when_db_raises(self):
        def boom(**kwargs):
            raise RuntimeError("db down")

        db = SimpleNamespace(get_analysis_history=boom)
        assert load_previous_analysis_context(db, "600519") is None


class TestFormatPreviousAnalysisSection:
    def test_zh_renders_conditions_points_and_verify_instruction(self):
        prev = {
            "analysis_time": datetime(2026, 8, 9, 10, 0, 0),
            "operation_advice": "持有观察",
            "watch_conditions": ["跌破 1450 止损离场", "放量突破 1620 再加仓"],
            "next_check_time": "2026-08-11 09:30",
            "ideal_buy": 1500.0,
            "stop_loss": 1450.0,
            "analysis_summary": "资金未确认，先观察。",
        }
        text = format_previous_analysis_section(prev, "zh")
        assert "上次分析观察点" in text
        assert "2026-08-09 10:00" in text
        assert "持有观察" in text
        assert "跌破 1450 止损离场" in text
        assert "止损位 1450" in text
        assert "理想买点 1500" in text
        assert "已兑现" in text

    def test_en_renders_with_english_labels(self):
        prev = {"watch_conditions": ["break below 12.5 stop"], "stop_loss": 12.5}
        text = format_previous_analysis_section(prev, "en")
        assert "Previous watch points" in text
        assert "break below 12.5 stop" in text
        assert "stop-loss 12.5" in text
        assert "fulfilled" in text

    def test_empty_prev_returns_empty(self):
        assert format_previous_analysis_section({}, "zh") == ""
        assert format_previous_analysis_section(None, "zh") == ""

    def test_caps_watch_condition_length(self):
        long_condition = "条" * 300
        prev = {"watch_conditions": [long_condition]}
        text = format_previous_analysis_section(prev, "zh")
        assert "条" * 120 + "…" in text
        rendered = [line for line in text.splitlines() if line.startswith("  - ")][0]
        assert len(rendered) <= 126  # "  - " (4) + 120 chars + "…" (1) + margin


class TestPromptRenderingIntegration:
    def _make_analyzer(self):
        from src.analyzer import GeminiAnalyzer

        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._router = None
        analyzer._litellm_available = True
        analyzer._config_override = SimpleNamespace(
            report_language="zh",
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        analyzer._skill_instructions_override = ""
        analyzer._default_skill_policy_override = ""
        analyzer._use_legacy_default_prompt_override = False
        analyzer._resolved_prompt_state = None
        return analyzer

    def test_analyzer_format_prompt_injects_previous_section(self):
        analyzer = self._make_analyzer()
        context = {
            "code": "600519",
            "stock_name": "贵州茅台",
            "today": {"close": 1560.0},
            "yesterday": {"close": 1545.0},
            "previous_analysis_context": (
                "## ⏮️ 上次分析观察点（请核对兑现情况）\n"
                "- 上次分析时间: 2026-08-09 10:00\n"
                "- 上次观察条件:\n"
                "  - 跌破 1450 止损离场\n"
                "> 请逐条核对以上「上次观察条件」"
            ),
        }
        prompt = analyzer._format_prompt(context, "贵州茅台", None, report_language="zh")
        assert "上次分析观察点" in prompt
        assert "跌破 1450 止损离场" in prompt
        # previous section precedes the news section
        assert prompt.index("上次分析观察点") < prompt.index("舆情情报")

    def test_agent_user_message_injects_previous_section(self):
        from src.agent.executor import AgentExecutor

        context = {
            "stock_code": "600519",
            "report_type": "stock_analysis",
            "report_language": "zh",
            "previous_analysis_context": (
                "## ⏮️ 上次分析观察点（请核对兑现情况）\n"
                "- 上次观察条件:\n"
                "  - 跌破 1450 止损离场\n"
            ),
        }
        message = AgentExecutor._build_user_message(
            object(), "请分析股票 600519", context
        )
        assert "上次分析观察点" in message
        assert "跌破 1450 止损离场" in message
