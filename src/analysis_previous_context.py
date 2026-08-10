# -*- coding: utf-8 -*-
"""Reuse the previous analysis's watch points in the next stock analysis run.

The daily analysis pipeline already persists each report's ``watch_conditions``
(``dashboard.phase_decision.watch_conditions``), sniper points
(``ideal_buy`` / ``secondary_buy`` / ``stop_loss`` / ``take_profit`` columns)
and operation advice into ``analysis_history``. Nothing reads them back, so the
next run starts blind: a condition such as "跌破 12.50 止损则离场" is never
checked against today's price action and the key signal silently drops.

This module is the read-back + render layer shared by the legacy LLM path and
the Agent path:

- ``load_previous_analysis_context`` selects the most recent eligible record.
- ``format_previous_analysis_section`` renders a compact prompt section that
  asks the LLM to verify each condition. This is a *soft* constraint today: the
  instruction is in the prompt but missing verification does not fail the
  report integrity check. The strong-constraint upgrade (a structured
  ``previous_watch_verification`` field validated by the report integrity
  checker) is deliberately not implemented yet; see the evaluation in
  docs/CHANGELOG.md.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.report_language import normalize_report_language

logger = logging.getLogger(__name__)

# Maximum record age considered reusable, in calendar days.
_DEFAULT_MAX_AGE_DAYS = 10
# Per-field caps to keep the injected section small.
_MAX_WATCH_CONDITIONS = 5
_MAX_CONDITION_CHARS = 120
_MAX_SUMMARY_CHARS = 120
# Skip records that are market reviews; the daily-market-context channel
# already carries that signal into the prompt.
_SKIP_REPORT_TYPES = frozenset({"market_review"})

_SNIPER_KEYS = ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")


def load_previous_analysis_context(
    db: Any,
    code: str,
    *,
    exclude_query_id: Optional[str] = None,
    now: Optional[datetime] = None,
    max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    limit: int = 20,
) -> Optional[Dict[str, Any]]:
    """Return the newest reusable analysis record for ``code``, or None.

    Eligibility:
    - ``created_at`` strictly before ``now`` (a not-yet-saved run cannot be its
      own previous), but the *same trading day* is allowed so an intraday rerun
      sees the pre-market analysis;
    - ``report_type`` not in ``_SKIP_REPORT_TYPES``;
    - not the record of ``exclude_query_id`` (the current run / same query).
    """
    now = now or datetime.now()
    try:
        records = db.get_analysis_history(
            code=code,
            days=max_age_days,
            limit=limit,
            exclude_query_id=exclude_query_id,
        )
    except Exception as exc:
        logger.warning("读取上次分析失败（fail-open）: code=%s err=%s", code, exc)
        return None

    for record in records:
        created = getattr(record, "created_at", None)
        if created is None or created >= now:
            continue
        report_type = getattr(record, "report_type", None)
        if report_type in _SKIP_REPORT_TYPES:
            continue
        prev = _extract_from_record(record)
        if prev is not None:
            return prev
    return None


def format_previous_analysis_section(
    prev: Optional[Dict[str, Any]],
    report_language: str = "zh",
) -> str:
    """Render the previous watch-point section, or "" when nothing reusable."""
    if not isinstance(prev, dict) or not _has_content(prev):
        return ""
    language = normalize_report_language(report_language)
    labels = _LABELS.get(language, _LABELS["zh"])
    lines = [labels["heading"]]
    analysis_time = prev.get("analysis_time")
    if isinstance(analysis_time, datetime):
        lines.append(f"- {labels['time']}: {analysis_time.strftime('%Y-%m-%d %H:%M')}")
    advice = prev.get("operation_advice")
    if advice:
        lines.append(f"- {labels['advice']}: {advice}")
    conditions = prev.get("watch_conditions") or []
    if conditions:
        lines.append(f"- {labels['conditions']}:")
        for condition in conditions:
            lines.append(f"  - {_truncate(condition, _MAX_CONDITION_CHARS)}")
    points = _format_sniper_points(prev, language)
    if points:
        lines.append(f"- {labels['points']}: {points}")
    next_check = prev.get("next_check_time")
    if next_check:
        lines.append(f"- {labels['next_check']}: {next_check}")
    summary = prev.get("analysis_summary")
    if summary:
        lines.append(f"- {labels['summary']}: {summary}")
    lines.append(f"> {labels['verify']}")
    return "\n".join(lines) + "\n"


def _has_content(prev: Dict[str, Any]) -> bool:
    return any(
        prev.get(key)
        for key in (
            "analysis_time",
            "operation_advice",
            "analysis_summary",
            "next_check_time",
            "watch_conditions",
            *_SNIPER_KEYS,
        )
    )


def _extract_from_record(record: Any) -> Optional[Dict[str, Any]]:
    watch_conditions: List[str] = []
    next_check_time: Optional[str] = None
    raw_result = _parse_json(getattr(record, "raw_result", None))
    if isinstance(raw_result, dict):
        dashboard = raw_result.get("dashboard")
        if isinstance(dashboard, dict):
            watch_conditions, next_check_time = _watch_from_dashboard(dashboard)

    sniper: Dict[str, Optional[float]] = {}
    for key in _SNIPER_KEYS:
        value = getattr(record, key, None)
        if value is not None:
            try:
                sniper[key] = float(value)
            except (TypeError, ValueError):
                sniper[key] = None

    advice = getattr(record, "operation_advice", None)
    summary = _truncate(getattr(record, "analysis_summary", None), _MAX_SUMMARY_CHARS)

    if not watch_conditions and not any(sniper.values()) and not advice and not summary:
        return None

    return {
        "analysis_time": getattr(record, "created_at", None),
        "operation_advice": advice,
        "analysis_summary": summary,
        "watch_conditions": watch_conditions[:_MAX_WATCH_CONDITIONS],
        "next_check_time": next_check_time,
        **sniper,
    }


def _watch_from_dashboard(dashboard: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
    conditions: List[str] = []
    next_check: Optional[str] = None
    phase_decision = dashboard.get("phase_decision")
    if isinstance(phase_decision, dict):
        raw = phase_decision.get("watch_conditions")
        if isinstance(raw, list):
            conditions = [str(x).strip() for x in raw if str(x or "").strip()]
        nct = phase_decision.get("next_check_time")
        if isinstance(nct, str) and nct.strip():
            next_check = nct.strip()
    if not conditions:
        battle_plan = dashboard.get("battle_plan")
        if isinstance(battle_plan, dict):
            checklist = battle_plan.get("action_checklist")
            if isinstance(checklist, list):
                conditions = [str(x).strip() for x in checklist if str(x or "").strip()]
    return conditions, next_check


def _format_sniper_points(prev: Dict[str, Any], language: str) -> str:
    labels = _POINT_LABELS.get(language, _POINT_LABELS["zh"])
    parts = []
    for key, label in labels:
        value = prev.get(key)
        if value is not None:
            parts.append(f"{label} {value:g}")
    return " / ".join(parts)


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _parse_json(value: Any) -> Optional[Any]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


_LABELS = {
    "zh": {
        "heading": "## ⏮️ 上次分析观察点（请核对兑现情况）",
        "time": "上次分析时间",
        "advice": "上次操作建议",
        "conditions": "上次观察条件",
        "points": "上次狙击点位",
        "next_check": "上次给出的下次检查",
        "summary": "上次分析摘要",
        "verify": (
            "请逐条核对以上「上次观察条件」在今日行情/最新数据中的兑现情况"
            "（例如止损位是否已跌破、压力位是否有效突破、量能与资金是否配合），"
            "对每一条明确标注「已兑现 / 未兑现 / 部分兑现」，并说明其对本次决策的影响。"
            "若上次分析距今超过 10 个交易日，可判定观察点已失效并直接说明，不必强行引用。"
        ),
    },
    "en": {
        "heading": "## ⏮️ Previous watch points (verify before concluding)",
        "time": "Previous analysis time",
        "advice": "Previous operation advice",
        "conditions": "Previous watch conditions",
        "points": "Previous sniper points",
        "next_check": "Previous next check",
        "summary": "Previous summary",
        "verify": (
            "Check each previous watch condition against today's price action / latest data "
            "(e.g. whether the stop-loss was breached, resistance was broken, or volume / capital "
            "flow confirmed), mark each as \"fulfilled / not fulfilled / partially fulfilled\", and "
            "state the impact on today's conclusion. If the previous analysis is older than 10 "
            "calendar days, treat the watch points as stale and say so instead of force-citing them."
        ),
    },
    "ko": {
        "heading": "## ⏮️ 이전 분석 관찰 포인트 (이행 여부 확인)",
        "time": "이전 분석 시간",
        "advice": "이전 매매 권고",
        "conditions": "이전 관찰 조건",
        "points": "이전 저격 포인트",
        "next_check": "이전 다음 점검",
        "summary": "이전 요약",
        "verify": (
            "위의 이전 관찰 조건 각각을 오늘 시세/최신 데이터와 대조해 "
            "(예: 손절선 이탈 여부, 저항선 돌파 여부, 거래량·자금 동반 여부) "
            "「이행됨 / 미이행 / 부분 이행」으로 명시하고 이번 판단에 미치는 영향을 서술하세요. "
            "이전 분석이 10일 이상 지났다면 관찰 포인트가 유효하지 않다고 판단하고 그렇게 밝히세요."
        ),
    },
}

_POINT_LABELS = {
    "zh": (
        ("ideal_buy", "理想买点"),
        ("secondary_buy", "次级买点"),
        ("stop_loss", "止损位"),
        ("take_profit", "止盈位"),
    ),
    "en": (
        ("ideal_buy", "ideal buy"),
        ("secondary_buy", "secondary buy"),
        ("stop_loss", "stop-loss"),
        ("take_profit", "take-profit"),
    ),
    "ko": (
        ("ideal_buy", "이상 매수가"),
        ("secondary_buy", "2차 매수가"),
        ("stop_loss", "손절선"),
        ("take_profit", "익절선"),
    ),
}
