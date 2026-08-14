# -*- coding: utf-8 -*-
"""Phase-aware decision guardrails for Issue #1386 P5."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.analysis_context_pack_prompt import CORE_DEGRADED_STATUSES
from src.market_phase_summary import render_market_phase_summary
from src.report_language import localize_confidence_level, normalize_report_language

if TYPE_CHECKING:
    from src.analyzer import AnalysisResult


INTRADAY_PHASES = {"intraday", "lunch_break", "closing_auction"}
CONSERVATIVE_ACTION_PHASES = {"premarket", "non_trading", "unknown"}
CORE_DATA_BLOCKS = {"quote", "daily_bars", "technical"}

PHASE_CONTEXT_KEYS = (
    "phase",
    "market",
    "market_local_time",
    "session_date",
    "effective_daily_bar_date",
    "is_trading_day",
    "is_market_open_now",
    "is_partial_bar",
    "minutes_to_open",
    "minutes_to_close",
    "trigger_source",
    "analysis_intent",
    "warnings",
)

_ZH_POSTMARKET_RECAP_PATTERNS = (
    "今日收盘后",
    "收盘后复盘",
    "盘后复盘",
    "明日重点关注",
    "明天重点关注",
    "完整交易日复盘",
)

_EN_POSTMARKET_RECAP_PATTERNS = (
    "after today's close",
    "after today’s close",
    "after the close",
    "post-market recap",
    "post market recap",
    "focus tomorrow",
    "tomorrow's focus",
    "tomorrow’s focus",
)

_IMMEDIATE_ACTION_MARKERS_ZH = (
    "立即买入",
    "马上买入",
    "立即加仓",
    "马上加仓",
    "立即卖出",
    "马上卖出",
    "立即减仓",
    "马上减仓",
)
_IMMEDIATE_ACTION_MARKERS_EN = ("buy now", "sell now", "immediate buy", "immediate sell", "add now", "reduce now")
_NEGATION_PREFIXES_ZH = ("暂不", "不建议", "禁止", "不要", "无需", "避免", "不能", "不可", "不宜", "勿", "不")
_NEGATION_PREFIXES_EN = ("do not", "don't", "dont", "not", "no", "avoid", "hold off", "without")

_KO_POSTMARKET_RECAP_PATTERNS = (
    "오늘 장 마감 후",
    "장 마감 후 리뷰",
    "장 마감 후",
    "마감 후 리뷰",
    "내일 주목",
    "내일 집중",
    "완전한 거래일 리뷰",
)
_IMMEDIATE_ACTION_MARKERS_KO = ("즉시 매수", "지금 매수", "즉시 비중확대", "즉시 매도", "지금 매도", "즉시 비중축소")
_NEGATION_PREFIXES_KO = ("하지", "권하지 않", "금지", "삼가", "불필요", "피하", "불가", "않", "안")


def _recap_patterns_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _EN_POSTMARKET_RECAP_PATTERNS
    if language == "ko":
        return _KO_POSTMARKET_RECAP_PATTERNS
    return _ZH_POSTMARKET_RECAP_PATTERNS


def _immediate_markers_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _IMMEDIATE_ACTION_MARKERS_EN
    if language == "ko":
        return _IMMEDIATE_ACTION_MARKERS_KO
    return _IMMEDIATE_ACTION_MARKERS_ZH


def _negations_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _NEGATION_PREFIXES_EN
    if language == "ko":
        return _NEGATION_PREFIXES_KO
    return _NEGATION_PREFIXES_ZH


def _reason_text(language: str, *, en: str, zh: str, ko: str) -> str:
    if language == "en":
        return en
    if language == "ko":
        return ko
    return zh


def apply_phase_decision_guardrails(
    result: "AnalysisResult",
    *,
    market_phase_summary: Optional[Dict[str, Any]],
    analysis_context_pack_overview: Optional[Dict[str, Any]],
    report_language: str = "zh",
) -> List[str]:
    """Apply phase/data-quality guardrails to an AnalysisResult in place."""

    if result is None:
        return []

    language = normalize_report_language(report_language or getattr(result, "report_language", "zh"))
    phase_summary = _safe_phase_summary(market_phase_summary)
    overview = analysis_context_pack_overview if isinstance(analysis_context_pack_overview, Mapping) else None
    adjustments: List[str] = []

    dashboard_value = getattr(result, "dashboard", None)
    if not isinstance(dashboard_value, dict):
        dashboard_value = {}
        setattr(result, "dashboard", dashboard_value)
    dashboard = dashboard_value
    phase_decision = dashboard.get("phase_decision")
    if not isinstance(phase_decision, dict):
        phase_decision = {}
    dashboard["phase_decision"] = phase_decision

    _ensure_phase_decision_shape(phase_decision)

    if phase_summary:
        phase_decision["phase_context"] = _phase_context_from_summary(phase_summary)

    objective_limitations = _overview_limitations(overview)
    model_limitations = _filter_model_limitations(
        _list_strings(phase_decision.get("data_limitations")),
        phase_summary=phase_summary,
        overview=overview,
        objective_limitations=objective_limitations,
        language=language,
    )
    merged_limitations = _merge_limitations(
        objective_limitations,
        _phase_warning_limitations(phase_summary, language=language),
        model_limitations,
    )
    phase_decision["data_limitations"] = merged_limitations

    phase = _safe_text(phase_summary.get("phase")) if phase_summary else ""
    core_degraded = _has_core_degraded_block(overview)
    initially_high_confidence = _is_high_confidence(getattr(result, "confidence_level", ""))

    if core_degraded and initially_high_confidence:
        result.confidence_level = localize_confidence_level("medium", language)
        reason = _reason_text(
            language,
            en="Core quote, daily-bar, or technical data is degraded; high confidence was capped.",
            zh="核心行情、日线或技术数据受限，已限制高置信结论。",
            ko="핵심 시세·일봉·기술 데이터가 제한되어 높은 신뢰도를 하향 조정했습니다.",
        )
        _append_reason(phase_decision, reason)
        adjustments.append("confidence_capped_core_data_degraded")

    has_non_intraday_action = (
        phase in CONSERVATIVE_ACTION_PHASES
        and _has_immediate_buy_sell_signal(result, phase_decision, language=language)
    )
    if has_non_intraday_action:
        phase_decision["immediate_action"] = _safe_wait_action(language)
        reason = _reason_text(
            language,
            en="Current market phase does not support immediate intraday buy/sell action.",
            zh="当前市场阶段不支持即时盘中买卖动作。",
            ko="현재 시장 단계에서는 즉시 장중 매수/매도 동작을 지원하지 않습니다.",
        )
        _append_reason(phase_decision, reason)
        adjustments.append("non_intraday_action_adjusted")
        if initially_high_confidence:
            result.confidence_level = localize_confidence_level("low", language)
            adjustments.append("confidence_capped_non_intraday_action")

    if phase in INTRADAY_PHASES and _contains_postmarket_recap(result, phase_decision, language=language):
        reason = _reason_text(
            language,
            en="Intraday output contained post-market recap wording; replaced with phase-safe action wording.",
            zh="盘中输出包含盘后复盘口吻，已替换为阶段安全动作表述。",
            ko="장중 출력에 장 마감 후 리뷰 표현이 있어 단계에 맞는 안전한 표현으로 교체했습니다.",
        )
        _replace_postmarket_recap_fields(result, phase_decision, language=language)
        _append_reason(phase_decision, reason)
        adjustments.append("postmarket_recap_wording_adjusted")

    if adjustments:
        phase_decision["data_limitations"] = _merge_limitations(
            phase_decision.get("data_limitations"),
            [_adjustment_limitation_text(item, language=language) for item in adjustments],
        )

    return adjustments


def _ensure_phase_decision_shape(phase_decision: Dict[str, Any]) -> None:
    phase_decision.setdefault("phase_context", None)
    phase_decision.setdefault("action_window", None)
    phase_decision.setdefault("immediate_action", None)
    phase_decision["watch_conditions"] = _list_strings(phase_decision.get("watch_conditions"))
    phase_decision.setdefault("next_check_time", None)
    phase_decision.setdefault("confidence_reason", None)
    phase_decision["data_limitations"] = _list_strings(phase_decision.get("data_limitations"))


def _safe_phase_summary(value: Any) -> Optional[Dict[str, Any]]:
    summary = render_market_phase_summary(value)
    if not summary:
        return None
    return summary


def _phase_context_from_summary(summary: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: summary.get(key) for key in PHASE_CONTEXT_KEYS if key in summary}


def _has_core_degraded_block(overview: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(overview, Mapping):
        return False
    blocks = overview.get("blocks")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        key = _safe_text(block.get("key"))
        status = _safe_text(block.get("status"))
        if key in CORE_DATA_BLOCKS and status in CORE_DEGRADED_STATUSES:
            return True
    return False


def _overview_limitations(overview: Optional[Mapping[str, Any]]) -> List[str]:
    if not isinstance(overview, Mapping):
        return []
    data_quality = overview.get("data_quality")
    if not isinstance(data_quality, Mapping):
        return []
    return _list_strings(data_quality.get("limitations"))


_MISSING_DATA_MARKERS = (
    "未包含",
    "未提供",
    "未获取",
    "无法获取",
    "无法获得",
    "数据缺失",
    "缺少",
    "缺失",
    "缺乏",
    "不足",
    "没有",
    "missing",
    "not available",
    "unavailable",
    "not provided",
    "could not obtain",
    "누락",
    "없음",
    "제공되지",
)
_LIMITATION_BLOCK_KEYWORDS = {
    "quote": ("实时行情", "实时报价", "实时价格", "当前价格", "盘口", "成交额", "量比", "换手率", "振幅"),
    "daily_bars": ("日线", "日 K", "日K", "收盘价", "开高低收", "OHLC", "daily bar"),
    "technical": ("技术指标", "均线", "MACD", "RSI", "KDJ", "布林", "technical indicator"),
    "news": ("新闻", "舆情", "资讯", "news", "sentiment"),
    "fundamentals": (
        "主力资金",
        "资金净流",
        "融资融券",
        "基本面",
        "财务",
        "估值",
        "机构持仓",
        "大宗交易",
        "股东行为",
        "capital flow",
        "margin balance",
        "fundamental",
    ),
    "chip": ("筹码", "chip distribution"),
}
_POSTMARKET_FALSE_LIMITATION_MARKERS = (
    "尚未收盘",
    "市场未收盘",
    "当日未收盘",
    "收盘数据尚未",
    "market has not closed",
    "before market close",
    "장 마감 전",
)
_INTRADAY_DETAIL_MARKERS = (
    "分时",
    "逐笔",
    "分钟级",
    "intraday series",
    "tick data",
    "minute bars",
    "분봉",
    "체결",
)


def _filter_model_limitations(
    limitations: List[str],
    *,
    phase_summary: Optional[Mapping[str, Any]],
    overview: Optional[Mapping[str, Any]],
    objective_limitations: List[str],
    language: str,
) -> List[str]:
    """Drop model claims contradicted by deterministic phase/context metadata."""
    phase = _safe_text(phase_summary.get("phase")) if isinstance(phase_summary, Mapping) else ""
    block_statuses = _overview_block_statuses(overview)
    objective_keys = {
        item.split(":", 1)[0].strip()
        for item in objective_limitations
        if ":" in item
    }
    filtered: List[str] = []
    for limitation in limitations:
        lowered = limitation.lower()
        if phase == "postmarket" and any(
            marker.lower() in lowered for marker in _POSTMARKET_FALSE_LIMITATION_MARKERS
        ):
            continue
        claims_missing = any(marker.lower() in lowered for marker in _MISSING_DATA_MARKERS)
        if claims_missing and any(
            marker.lower() in lowered for marker in _INTRADAY_DETAIL_MARKERS
        ):
            canonical = _reason_text(
                language,
                en="Minute/tick-level intraday series are unavailable; intraday path and persistence cannot be verified.",
                zh="未提供分钟级分时/逐笔成交序列，无法核验盘中路径与持续性",
                ko="분봉/체결 단위 장중 시계열이 없어 장중 경로와 지속성을 확인할 수 없습니다.",
            )
            if canonical not in filtered:
                filtered.append(canonical)
            continue
        if claims_missing:
            contradicted = False
            for key, keywords in _LIMITATION_BLOCK_KEYWORDS.items():
                if not any(keyword.lower() in lowered for keyword in keywords):
                    continue
                status = block_statuses.get(key)
                if status == "available" or (
                    status == "partial" and key in objective_keys
                ):
                    contradicted = True
                    break
            if contradicted:
                continue
        filtered.append(limitation)
    return filtered


def _overview_block_statuses(
    overview: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    if not isinstance(overview, Mapping):
        return {}
    blocks = overview.get("blocks")
    if not isinstance(blocks, list):
        return {}
    statuses: Dict[str, str] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        key = _safe_text(block.get("key"))
        status = _safe_text(block.get("status"))
        if key and status:
            statuses[key] = status
    return statuses


def _phase_warning_limitations(summary: Optional[Mapping[str, Any]], *, language: str) -> List[str]:
    if not isinstance(summary, Mapping):
        return []
    warnings = _list_strings(summary.get("warnings"))
    if not warnings:
        return []
    if language == "en":
        return [f"market phase warning: {item}" for item in warnings]
    if language == "ko":
        return [f"시장 단계 경고: {item}" for item in warnings]
    return [f"市场阶段提醒：{item}" for item in warnings]


def _merge_limitations(*groups: Any, limit: int = 5) -> List[str]:
    merged: List[str] = []
    for group in groups:
        for item in _list_strings(group):
            if item not in merged:
                merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def _is_high_confidence(value: Any) -> bool:
    text = _safe_text(value).lower()
    return text in {"高", "high", "높음"}


def _has_immediate_buy_sell_signal(
    result: "AnalysisResult",
    phase_decision: Mapping[str, Any],
    *,
    language: str,
) -> bool:
    haystack = " ".join(
        _safe_text(value)
        for value in (
            getattr(result, "operation_advice", ""),
            phase_decision.get("immediate_action"),
        )
    ).lower()
    immediate_markers = _immediate_markers_for(language)
    if _contains_non_negated_marker(haystack, immediate_markers, language=language):
        return True
    return _safe_text(getattr(result, "decision_type", "")).lower() in {"buy", "sell"}


def _contains_non_negated_marker(text: str, markers: tuple[str, ...], *, language: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    for marker in markers:
        marker_text = marker.lower()
        start = 0
        while True:
            index = lowered.find(marker_text, start)
            if index < 0:
                break
            if not _is_negated_marker(lowered, index, language=language):
                return True
            start = index + len(marker_text)
    return False


def _is_negated_marker(text: str, marker_index: int, *, language: str) -> bool:
    window = 24 if language == "en" else 8
    prefix = text[max(0, marker_index - window):marker_index].rstrip()
    negations = _negations_for(language)
    return any(prefix.endswith(item) for item in negations)


def _contains_postmarket_recap(result: "AnalysisResult", phase_decision: Mapping[str, Any], *, language: str) -> bool:
    dashboard_value = getattr(result, "dashboard", None)
    dashboard = dashboard_value if isinstance(dashboard_value, dict) else {}
    core = dashboard.get("core_conclusion")
    core = core if isinstance(core, Mapping) else {}
    values = (
        core.get("one_sentence"),
        getattr(result, "operation_advice", ""),
        getattr(result, "analysis_summary", ""),
        phase_decision.get("immediate_action"),
    )
    patterns = _recap_patterns_for(language)
    return any(_contains_any(value, patterns) for value in values)


def _replace_postmarket_recap_fields(
    result: "AnalysisResult",
    phase_decision: Dict[str, Any],
    *,
    language: str,
) -> None:
    dashboard_value = getattr(result, "dashboard", None)
    dashboard = dashboard_value if isinstance(dashboard_value, dict) else {}
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    safe_action = _safe_wait_action(language)
    safe_summary = _reason_text(
        language,
        en=(
            "This is an intraday phase; use live state, watch conditions, and the next "
            "check point rather than post-market recap wording."
        ),
        zh="当前处于盘中阶段，应以实时状态、观察条件和下一次检查点为准，避免盘后复盘口径。",
        ko="현재 장중 단계이므로 장 마감 후 리뷰 표현 대신 실시간 상태·관찰 조건·다음 점검 시점을 기준으로 합니다.",
    )
    if _contains_any(core.get("one_sentence"), _patterns(language)):
        core["one_sentence"] = safe_action
    if _contains_any(getattr(result, "operation_advice", ""), _patterns(language)):
        result.operation_advice = safe_action
    if _contains_any(getattr(result, "analysis_summary", ""), _patterns(language)):
        result.analysis_summary = safe_summary
    if _contains_any(phase_decision.get("immediate_action"), _patterns(language)):
        phase_decision["immediate_action"] = safe_action


def _append_reason(phase_decision: Dict[str, Any], reason: str) -> None:
    existing = _safe_text(phase_decision.get("confidence_reason"))
    if not existing:
        phase_decision["confidence_reason"] = reason
        return
    if reason not in existing:
        phase_decision["confidence_reason"] = f"{existing}；{reason}"


def _adjustment_limitation_text(adjustment: str, *, language: str) -> str:
    if adjustment == "postmarket_recap_wording_adjusted":
        return _reason_text(
            language,
            en="post-market recap wording adjusted",
            zh="已修正盘后复盘口吻",
            ko="장 마감 후 리뷰 표현을 수정함",
        )
    if adjustment == "non_intraday_action_adjusted":
        return _reason_text(
            language,
            en="non-intraday immediate action adjusted",
            zh="非盘中阶段已修正即时买卖动作",
            ko="비장중 단계의 즉시 매매 동작을 수정함",
        )
    if adjustment == "confidence_capped_non_intraday_action":
        return _reason_text(
            language,
            en="confidence capped for non-intraday action",
            zh="非盘中阶段已限制买卖置信度",
            ko="비장중 단계 매매에 대해 신뢰도를 제한함",
        )
    if adjustment == "confidence_capped_core_data_degraded":
        return _reason_text(
            language,
            en="confidence capped due to degraded core data",
            zh="核心数据受限已降低置信度",
            ko="핵심 데이터 제한으로 신뢰도를 낮춤",
        )
    return adjustment


def _safe_wait_action(language: str) -> str:
    return _reason_text(
        language,
        en="Wait for intraday confirmation; do not chase.",
        zh="等待盘中确认，禁止追高。",
        ko="장중 확인을 기다리고 추격 매수하지 마세요.",
    )


def _patterns(language: str) -> tuple[str, ...]:
    return _recap_patterns_for(language)


def _contains_any(value: Any, patterns: tuple[str, ...]) -> bool:
    text = _safe_text(value).lower()
    return bool(text) and any(pattern.lower() in text for pattern in patterns)


def _list_strings(value: Any, *, limit: int = 20) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = _safe_text(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        return ""
    return str(value).strip()
