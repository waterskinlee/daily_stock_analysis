# -*- coding: utf-8 -*-
"""Tests for the decision-context renderer."""

from src.decision_context import (
    localize_decision_signal,
    render_decision_context_section,
)


class TestRenderDecisionContextSection:
    def test_empty_dashboard_returns_empty(self):
        assert render_decision_context_section({}, "zh") == []
        assert render_decision_context_section(None, "zh") == []

    def test_strategy_synthesis_renders_final_signal_and_skills(self):
        dashboard = {
            "strategy_synthesis": {
                "final_signal": "buy",
                "consensus_level": "high",
                "conflict_severity": "medium",
                "conflict_count": 2,
                "confidence": 0.8,
                "supporting_skills": [{"skill_id": "bull_trend", "signal": "buy"}],
                "opposing_skills": [{"skill_id": "capital_heat", "signal": "hold"}],
            }
        }
        lines = render_decision_context_section(dashboard, "zh")
        assert lines
        text = "\n".join(lines)
        assert "决策上下文" in text
        assert "综合信号 买入" in text
        assert "共识度 high" in text
        assert "支持策略: bull_trend" in text
        assert "反方策略: capital_heat" in text
        assert "多策略综合为策略层共识" in text

    def test_risk_downgrade_renders_transition(self):
        dashboard = {
            "agent_disagreement_explanation": {
                "risk_control": {
                    "applied": True,
                    "from_signal": "buy",
                    "to_signal": "sell",
                    "post_risk_signal": "sell",
                    "trigger": "risk_downgrade",
                },
                "decision_path": "risk_downgrade",
            }
        }
        lines = render_decision_context_section(dashboard, "zh")
        text = "\n".join(lines)
        assert "风控下调" in text
        assert "由 买入 下调至 卖出" in text
        assert "决策路径" in text

    def test_degraded_renders_warning(self):
        dashboard = {
            "agent_disagreement_explanation": {
                "degraded_events": [{"stage": "skill", "reason": "timeout"}],
                "risk_control": {"applied": False, "post_risk_signal": "buy"},
            }
        }
        lines = render_decision_context_section(dashboard, "zh")
        text = "\n".join(lines)
        assert "数据降级" in text

    def test_en_localization(self):
        dashboard = {
            "strategy_synthesis": {
                "final_signal": "buy",
                "consensus_level": "high",
                "confidence": 0.8,
                "supporting_skills": [{"skill_id": "bull_trend", "signal": "buy"}],
            }
        }
        lines = render_decision_context_section(dashboard, "en")
        text = "\n".join(lines)
        assert "Final Signal Buy" in text
        assert "Consensus high" in text

    def test_only_decision_path_present_renders(self):
        dashboard = {
            "agent_disagreement_explanation": {
                "risk_control": {"applied": False, "post_risk_signal": "hold"},
                "decision_path": "mixed_signals_synthesized",
                "base_disagreement": {"type": "mixed_directional_signals"},
            }
        }
        lines = render_decision_context_section(dashboard, "zh")
        text = "\n".join(lines)
        assert "决策路径" in text
        assert "信号分歧综合" in text


class TestLocalizeDecisionSignal:
    def test_zh_mapping(self):
        assert localize_decision_signal("buy", "zh") == "买入"
        assert localize_decision_signal("sell", "zh") == "卖出"
        assert localize_decision_signal("hold", "zh") == "持有"

    def test_en_mapping(self):
        assert localize_decision_signal("buy", "en") == "Buy"

    def test_unknown_falls_back_to_raw(self):
        assert localize_decision_signal("watch", "zh") == "watch"
