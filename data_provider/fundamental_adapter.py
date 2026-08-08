# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import io
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    _EM_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    _EM_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://data.eastmoney.com/",
    }
    _THS_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://basic.10jqka.com.cn/",
    }
    _HTTP_TIMEOUT_SEC = 15

    def _em_datacenter_get(
        self,
        report_name: str,
        columns: str = "ALL",
        filter_str: str = "",
        page_size: int = 20,
        sort_columns: str = "",
        sort_types: str = "",
    ) -> List[Dict[str, Any]]:
        """Direct-HTTP Eastmoney datacenter query (free, no API key).

        Ported from tradingagents-astock (Apache-2.0)
        ``tradingagents/dataflows/a_stock.py::_eastmoney_datacenter``.
        """
        params: Dict[str, Any] = {
            "reportName": report_name,
            "columns": columns,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "source": "WEB",
            "client": "WEB",
        }
        if filter_str:
            # Pass the filter verbatim — the eastmoney datacenter API rejects
            # nested groups, so callers must supply fully parenthesized groups
            # (e.g. `(SECURITY_CODE="688783")(FREE_DATE>='..')(FREE_DATE<='..')`).
            params["filter"] = filter_str
        if sort_columns:
            params["sortColumns"] = sort_columns
            params["sortTypes"] = sort_types or "1"
        resp = requests.get(
            self._EM_DATACENTER_URL,
            params=params,
            headers=self._EM_HEADERS,
            timeout=self._HTTP_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("result") or {}
        data = result.get("data") or []
        return list(data)

    def _ths_consensus_eps(self, stock_code: str) -> Dict[str, Dict[str, Any]]:
        """Fetch 同花顺 consensus EPS forecast (direct HTTP, free).

        Ported from tradingagents-astock (Apache-2.0)
        ``tradingagents/dataflows/a_stock.py::_ths_eps_forecast``.
        Returns {year_str: {"count", "min", "mean", "max"}}; empty on failure.
        """
        url = f"https://basic.10jqka.com.cn/new/{stock_code}/worth.html"
        resp = requests.get(url, headers=self._THS_HEADERS, timeout=self._HTTP_TIMEOUT_SEC)
        resp.raise_for_status()
        resp.encoding = "gbk"
        dfs = pd.read_html(io.StringIO(resp.text))
        target_df: Optional[pd.DataFrame] = None
        for frame in dfs:
            cols = [str(c) for c in frame.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                target_df = frame
                break
        if target_df is None and dfs:
            target_df = dfs[0]
        if target_df is None or target_df.empty:
            return {}

        parsed: Dict[str, Dict[str, Any]] = {}
        for _, row in target_df.iterrows():
            if not isinstance(row, pd.Series):
                continue
            raw_year = _safe_str(row.iloc[0]) if len(row) > 0 else ""
            try:
                year_digits = str(int(float(raw_year)))
            except (TypeError, ValueError):
                year_digits = re.sub(r"\D", "", raw_year)
            if len(year_digits) != 4 or not year_digits.isdigit():
                continue
            year_str = year_digits
            def _cell(idx: int) -> Optional[float]:
                if len(row) <= idx:
                    return None
                value = _safe_float(row.iloc[idx])
                return value
            parsed[year_str] = {
                "count": _cell(1),
                "min": _cell(2),
                "mean": _cell(3),
                "max": _cell(4),
            }
        return parsed

    def get_lockup_schedule(
        self,
        stock_code: str,
        lookback_days: int = 180,
        forward_days: int = 90,
    ) -> Dict[str, Any]:
        """Eastmoney restricted-share unlock (限售解禁) calendar, direct HTTP.

        Fail-open: never raises. Returns block payload with
        ``history``/``upcoming`` lists and ``source_chain``/``errors``.
        """
        start_ts = time.time()
        code = _normalize_code(stock_code)
        today = datetime.now().date()
        lookback_start = (today - timedelta(days=max(1, int(lookback_days)))).isoformat()
        forward_end = (today + timedelta(days=max(1, int(forward_days)))).isoformat()
        errors: List[str] = []
        source_chain: List[Dict[str, Any]] = []
        history: List[Dict[str, Any]] = []
        upcoming: List[Dict[str, Any]] = []
        status = "ok"

        try:
            history_rows = self._em_datacenter_get(
                "RPT_LIFT_STAGE",
                filter_str=f'(SECURITY_CODE="{code}")',
                page_size=20,
                sort_columns="FREE_DATE",
                sort_types="-1",
            )
            source_chain.append(
                {
                    "provider": "eastmoney_lockup",
                    "result": "ok" if history_rows else "empty",
                    "duration_ms": int((time.time() - start_ts) * 1000),
                }
            )
            for row in history_rows:
                free_date = str(row.get("FREE_DATE") or "")[:10]
                if not free_date or free_date < lookback_start or free_date > today.isoformat():
                    continue
                history.append(
                    {
                        "free_date": free_date,
                        "share_type": str(row.get("FREE_SHARES_TYPE") or row.get("LIMITED_STOCK_TYPE") or ""),
                        "free_shares": _safe_float(row.get("FREE_SHARES") or row.get("FREE_SHARES_NUM")),
                        "free_ratio_pct": _safe_float(row.get("FREE_RATIO")),
                    }
                )
            history.sort(key=lambda item: item.get("free_date") or "", reverse=True)
        except Exception as exc:
            errors.append(f"lockup_history:{type(exc).__name__}")
            source_chain.append(
                {
                    "provider": "eastmoney_lockup",
                    "result": "failed",
                    "duration_ms": int((time.time() - start_ts) * 1000),
                    "error": type(exc).__name__,
                }
            )

        try:
            upcoming_rows = self._em_datacenter_get(
                "RPT_LIFT_STAGE",
                filter_str=(
                    f'(SECURITY_CODE="{code}")'
                    f"(FREE_DATE>='{today.isoformat()}')"
                    f"(FREE_DATE<='{forward_end}')"
                ),
                page_size=20,
                sort_columns="FREE_DATE",
                sort_types="1",
            )
            source_chain.append(
                {
                    "provider": "eastmoney_lockup",
                    "result": "ok" if upcoming_rows else "empty",
                    "duration_ms": int((time.time() - start_ts) * 1000),
                }
            )
            for row in upcoming_rows:
                free_date = str(row.get("FREE_DATE") or "")[:10]
                if not free_date or free_date < today.isoformat() or free_date > forward_end:
                    continue
                upcoming.append(
                    {
                        "free_date": str(row.get("FREE_DATE") or "")[:10],
                        "share_type": str(row.get("FREE_SHARES_TYPE") or row.get("LIMITED_STOCK_TYPE") or ""),
                        "free_shares": _safe_float(row.get("FREE_SHARES") or row.get("FREE_SHARES_NUM")),
                        "free_ratio_pct": _safe_float(row.get("FREE_RATIO")),
                    }
                )
            upcoming.sort(key=lambda item: item.get("free_date") or "")
        except Exception as exc:
            errors.append(f"lockup_upcoming:{type(exc).__name__}")
            source_chain.append(
                {
                    "provider": "eastmoney_lockup",
                    "result": "failed",
                    "duration_ms": int((time.time() - start_ts) * 1000),
                    "error": type(exc).__name__,
                }
            )

        if errors and not history and not upcoming:
            status = "failed"
        elif not history and not upcoming and not errors:
            status = "empty"

        return {
            "status": status,
            "history": history[:10],
            "upcoming": upcoming[:10],
            "source_chain": source_chain,
            "errors": errors,
            "as_of": today.isoformat(),
            "lookback_days": int(lookback_days),
            "forward_days": int(forward_days),
        }

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_institution_holding_change(self, stock_code: str) -> Optional[float]:
        """Return latest institution holding change via akshare stock_institute_hold.

        Used by the tushare bundle fallback: xiaodefa does not expose
        ``top_inst_hold``, so when institution_holding_change is missing we fill
        it from this keyless akshare feed (~0.7s, fail-open -> None).
        """
        detail = self.get_institution_holdings_detail(stock_code)
        return detail.get("institution_holding_change")

    def get_institution_holdings_detail(self, stock_code: str) -> Dict[str, Any]:
        """Return institution holdings detail for one A-share stock.

        Eastmoney zlsj (主力数据·机构持仓) first via datacenter-web
        ``RPT_MAIN_ORGHOLD`` (direct HTTP, no key, ~0.3s, full-market
        coverage — beats akshare's 605-row ``stock_institute_hold`` which
        omits many stocks). Falls back to akshare when zlsj has no row.

        Returns dict with institution_holding_change (增减数量), plus extra
        fields when available; empty dict on total failure.
        """
        result: Dict[str, Any] = {}
        try:
            # 最新报告期
            dates = self._em_datacenter_get(
                "RPT_MAIN_REPORTDATE",
                columns="REPORT_DATE",
                page_size=1,
                sort_columns="REPORT_DATE",
                sort_types="-1",
            )
            if dates:
                raw_date = str(dates[0].get("REPORT_DATE") or "")[:10]
                report_date = raw_date
            else:
                report_date = ""
            filter_str = f'(SECURITY_CODE="{stock_code}")'
            if report_date:
                filter_str += f"(REPORT_DATE='{report_date}')"
            rows = self._em_datacenter_get(
                "RPT_MAIN_ORGHOLD",
                columns="ALL",
                filter_str=filter_str,
                page_size=8,
            )
            if rows:
                # ORG_TYPE=00 机构汇总（若存在），否则取 01 基金
                pick = next(
                    (r for r in rows if str(r.get("ORG_TYPE")) == "00"),
                    None,
                )
                if pick is None:
                    pick = rows[0]
                ratio = _safe_float(pick.get("FREESHARES_RATIO"))
                ratio_change = _safe_float(pick.get("FREESHARES_RATIO_CHANGE"))
                num = _safe_float(pick.get("HOULD_NUM"))
                num_change = _safe_float(pick.get("HOLDCHA_NUM"))
                if ratio is not None or ratio_change is not None or num_change is not None:
                    if num_change is not None:
                        result["institution_holding_change"] = num_change
                    if num is not None:
                        result["institution_count"] = num
                    if ratio is not None:
                        result["institution_holding_ratio"] = ratio
                    if ratio_change is not None:
                        result["institution_ratio_change"] = ratio_change
                    direction = str(pick.get("HOLDCHA") or "").strip()
                    if direction:
                        result["hold_direction"] = direction
                    result["report_date"] = report_date
                    result["source"] = "eastmoney_zlsj"
                    # 股东户数（筹码集中度）：RPT_HOLDERNUMLATEST，同 datacenter 直连。
                    # 户数持续减少 = 筹码集中 = 主力吸筹信号（fail-open）。
                    try:
                        holder_rows = self._em_datacenter_get(
                            "RPT_HOLDERNUMLATEST",
                            columns="ALL",
                            filter_str=f'(SECURITY_CODE="{stock_code}")',
                            page_size=4,
                            sort_columns="END_DATE",
                            sort_types="-1",
                        )
                        if holder_rows:
                            h = holder_rows[0]
                            for key, src_key in (
                                ("holder_num", "HOLDER_NUM"),
                                ("holder_num_change", "HOLDER_NUM_CHANGE"),
                                ("holder_num_ratio", "HOLDER_NUM_RATIO"),
                            ):
                                val = _safe_float(h.get(src_key))
                                if val is not None:
                                    result[key] = val
                            if h.get("END_DATE"):
                                result["holder_report_date"] = str(h.get("END_DATE"))[:10]
                    except Exception as exc:  # noqa: BLE001 - fail-open
                        logger.debug(
                            "[AkshareFundamentalAdapter] 股东户数失败 %s: %s",
                            stock_code,
                            type(exc).__name__,
                        )
                    return result
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.debug(
                "[AkshareFundamentalAdapter] zlsj 机构持仓失败 %s: %s",
                stock_code,
                type(exc).__name__,
            )

        # 回退 akshare stock_institute_hold（机构数变化/持股比例增幅）
        inst_df, _inst_source, _inst_errors = self._call_df_candidates(
            [
                ("stock_institute_hold", {}),
                ("stock_institute_recommend", {}),
            ]
        )
        if inst_df is None:
            return {}
        row = _extract_latest_row(inst_df, stock_code)
        if row is None:
            return {}
        change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
        if change is not None:
            result["institution_holding_change"] = change
        result["source"] = "akshare_stock_institute_hold"
        return result

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Consensus EPS forecast is fetched by the fundamental-context layer as a
        # quick keyless block (see DataFetcherManager.get_fundamental_context),
        # so this bundle stays akshare-only and never blocks on the THS page.
        # (method _ths_consensus_eps remains for direct/standalone use)

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        stock_df, stock_source, stock_errors = self._call_df_candidates([
            ("stock_individual_fund_flow", {"stock": stock_code}),
            ("stock_individual_fund_flow", {"symbol": stock_code}),
            ("stock_individual_fund_flow", {}),
            ("stock_main_fund_flow", {"symbol": stock_code}),
            ("stock_main_fund_flow", {}),
        ])
        result["errors"].extend(stock_errors)
        if stock_df is not None:
            row = _extract_latest_row(stock_df, stock_code)
            if row is not None:
                net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                result["stock_flow"] = {
                    "main_net_inflow": net_inflow,
                    "inflow_5d": inflow_5d,
                    "inflow_10d": inflow_10d,
                }
                result["source_chain"].append(f"capital_stock:{stock_source}")

        sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {}),
            ("stock_sector_fund_flow_summary", {}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
