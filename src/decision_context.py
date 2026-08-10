# -*- coding: utf-8 -*-
"""Render the multi-agent decision explanation into report prose.

``AgentDisagreementExplanation`` (dashboard.agent_disagreement_explanation) is
generated after every Pipeline finalization but was never rendered anywhere.
This module localizes it into a compact section that explains why the final
decision differs from the strategy-layer consensus (e.g. risk downgrade).

Used by NotificationService, HistoryService and the Jinja templates so all
markdown report paths show the same decision context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from src.report_language import (
    get_report_labels,
    normalize_report_language,
)

# decision_path -> label key in report_language labels.
_DECISION_PATH_LABELS = {
    "aligned_agent_consensus": "aligned_agent_consensus_label",
    "mixed_signals_synthesized": "mixed_signals_synthesized_label",
    "limited_opinion_synthesis": "limited_opinion_synthesis_label",
    "degraded_synthesis": "degraded_synthesis_label",
    "risk_downgrade": "risk_downgrade_label",
    "risk_veto": "risk_downgrade_label",
    "downgrade_one": "risk_downgrade_label",
    "downgrade_two": "risk_downgrade_label",
    "high_risk_evidence": "risk_downgrade_label",
}


def _mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): val for key, val in value.items()}


def render_decision_context_section(
    dashboard: Any,
    report_language: str = "zh",
) -> List[str]:
    """Render the decision-context markdown lines, or [] when nothing to show."""
    dashboard = _mapping(dashboard)
    language = normalize_report_language(report_language)
    labels = get_report_labels(language)
    lines: List[str] = []

    explanation = _mapping(dashboard.get("agent_disagreement_explanation"))
    synthesis = _mapping(dashboard.get("strategy_synthesis"))
    risk_control = _mapping(explanation.get("risk_control"))
    risk_applied = bool(risk_control.get("applied"))
    final_adjustments = explanation.get("final_adjustments")
    adjustments = final_adjustments if isinstance(final_adjustments, list) else []
    degraded_events = explanation.get("degraded_events")
    degraded = isinstance(degraded_events, list) and bool(degraded_events)

    final_signal = synthesis.get("final_signal")
    consensus_level = synthesis.get("consensus_level")
    conflict_count = synthesis.get("conflict_count")
    supporting = synthesis.get("supporting_skills")
    opposing = synthesis.get("opposing_skills")
    decision_path = explanation.get("decision_path")
    has_decision_path = isinstance(decision_path, str) and bool(decision_path)
    has_synthesis = bool(
        final_signal
        and str(final_signal) not in ("", "N/A", "None")
    ) or bool(consensus_level)

    if not has_synthesis and not risk_applied and not adjustments and not degraded and not has_decision_path:
        return lines

    lines.append(f"### 🧩 {labels.get('decision_context_heading', '决策上下文')}")
    lines.append("")
    lines.append(f"> {labels.get('strategy_layer_note', '')}")
    lines.append("")

    if has_synthesis:
        final_text = localize_decision_signal(str(final_signal), language)
        consensus_text = str(consensus_level or "N/A")
        conflict_text = (
            f"{str(synthesis.get('conflict_severity') or 'N/A')}({int(conflict_count or 0)})"
            if conflict_count is not None
            else str(synthesis.get("conflict_severity") or "N/A")
        )
        confidence = synthesis.get("confidence")
        confidence_text = f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else "N/A"
        lines.append(
            f"- **{labels.get('strategy_synthesis_heading', '多策略综合')}**: "
            f"{labels.get('strategy_final_signal_label', '综合信号')} {final_text} | "
            f"{labels.get('strategy_consensus_level_label', '共识度')} {consensus_text} | "
            f"{labels.get('strategy_conflict_label', '冲突')} {conflict_text} | "
            f"{labels.get('strategy_confidence_label', '置信度')} {confidence_text}"
        )
        if isinstance(supporting, list) and supporting:
            names = [str(item.get("skill_id", "")) for item in supporting if isinstance(item, Mapping) and item.get("skill_id")]
            if names:
                lines.append(f"- {labels.get('strategy_supporting_skills_label', '支持策略')}: {'、'.join(names)}")
        if isinstance(opposing, list) and opposing:
            names = [str(item.get("skill_id", "")) for item in opposing if isinstance(item, Mapping) and item.get("skill_id")]
            if names:
                lines.append(f"- {labels.get('strategy_opposing_skills_label', '反方策略')}: {'、'.join(names)}")
        lines.append("")

    if risk_applied:
        from_signal = risk_control.get("from_signal")
        to_signal = risk_control.get("to_signal")
        from_text = localize_decision_signal(str(from_signal), language) if from_signal else "?"
        to_text = localize_decision_signal(str(to_signal), language) if to_signal else "?"
        transition = labels.get("risk_downgrade_transition", "")
        transition_text = transition.replace("{from}", from_text).replace("{to}", to_text)
        lines.append(
            f"- **{labels.get('risk_downgrade_label', '风控下调')}**: {transition_text}"
        )
        lines.append("")

    if degraded:
        lines.append(f"- **{labels.get('degraded_synthesis_label', '降级合成')}**: {labels.get('degraded_synthesis_warning', '本次分析存在数据降级或阶段异常，结论稳健性下降。')}")
        lines.append("")

    decision_path = explanation.get("decision_path")
    if has_decision_path:
        label_key = _DECISION_PATH_LABELS.get(decision_path, "decision_path_label")
        path_label = labels.get(label_key) or decision_path
        lines.append(f"- **{labels.get('decision_path_label', '决策路径')}**: {path_label}")
        lines.append("")

    return lines


def localize_decision_signal(signal: str, language: str = "zh") -> str:
    """Map a canonical decision signal to a display word."""
    mapping = {
        "zh": {"buy": "买入", "hold": "持有", "sell": "卖出"},
        "en": {"buy": "Buy", "hold": "Hold", "sell": "Sell"},
        "ko": {"buy": "매수", "hold": "보유", "sell": "매도"},
    }
    table = mapping.get(language, mapping["zh"])
    return table.get(str(signal).lower(), str(signal))
