# -*- coding: utf-8 -*-
"""Regression contracts for the daily K-line screening chain fixes.

Covers:
- daily enrichment pool widening gated on parallel fetch workers, with
  explicit degradation for pool truncation and inactive widening;
- the explicit excluded-tail note when non-daily candidates drop out of
  downstream ranking;
- risk-overlay reserve wiring from below-the-ranking-cut candidates;
- optional LLM ranking prompt-cap pass-through (None keeps ranker default);
- RSI zero-loss edge cases (all-rising -> 100, flat -> 50);
- synthetic-OHLC tracking in daily quality scoring.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from src.services.screening import pipeline as screening_pipeline
from src.services.screening.config import Config as ScreeningRuntimeConfig
from src.services.screening.daily import (
    _compute_daily_quality,
    _compute_rsi,
    _normalize_daily_history,
    compute_daily_features,
)
from src.services.screening.models import HardFilterConfig, ScreeningConfig, Strategy
from src.services.screening.ranker import LLMRankingResult
from src.services.screening.risk import apply_risk_overlay as real_apply_risk_overlay

REPO_ROOT = Path(__file__).resolve().parents[1]
SCREENING_ROOT = REPO_ROOT / "src" / "services" / "screening"


def _snapshot_df(count: int) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "code": f"{600000 + index:06d}",
                "name": f"Stock{index}",
                "price": 10.0 + index,
                "change_pct": 1.0,
                "amount": 200_000_000.0 + index,
            }
            for index in range(count)
        ]
    )
    frame.attrs.update(
        {"snapshot_source": "sina", "source_errors": [], "fallback_used": False}
    )
    return frame


def _demo_strategy() -> Strategy:
    return Strategy(
        name="demo",
        display_name="Demo",
        description="demo",
        screening=ScreeningConfig(
            enabled=True,
            market_scope=["cn"],
            hard_filters=HardFilterConfig(),
            factor_weights={"value": 1.0},
            max_output=3,
        ),
    )


def _base_config(**overrides: object) -> ScreeningRuntimeConfig:
    values: dict[str, object] = {
        "strategies_dir": SCREENING_ROOT / "strategies",
        "daily_enrich_enabled": True,
        "post_analyzers": [],
        "risk_enabled": True,
        "portfolio_diversity_enabled": False,
    }
    values.update(overrides)
    return ScreeningRuntimeConfig(**values)


def _install_pipeline_stubs(monkeypatch, *, capture: dict[str, object]) -> None:
    def fake_enrich_daily_features(df: pd.DataFrame, **kwargs):
        capture["enriched_rows"] = len(df)
        capture["max_rows"] = kwargs.get("max_rows")
        enriched = df.copy()
        enriched.attrs.update(
            {
                "daily_errors": [],
                "daily_success_count": len(enriched),
                "daily_source_counts": {"cache": len(enriched)},
                "daily_quality_flag_counts": {},
                "daily_source_order_notes": [],
                "daily_source_health": {},
            }
        )
        return enriched

    monkeypatch.setattr(
        screening_pipeline, "load_all_strategies", lambda _path: {"demo": _demo_strategy()}
    )
    monkeypatch.setattr(
        screening_pipeline,
        "fetch_snapshot_with_fallback",
        lambda *args, **kwargs: _snapshot_df(capture["candidate_count"]).copy(),
    )
    monkeypatch.setattr(
        screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy()
    )
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(
            screen_score=[100.0 - index for index in range(len(df))]
        ),
    )
    monkeypatch.setattr(
        screening_pipeline, "enrich_daily_features", fake_enrich_daily_features
    )
    monkeypatch.setattr(
        screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: []
    )
    monkeypatch.setattr(
        screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, [])
    )
    monkeypatch.setattr(
        screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, [])
    )


def test_parallel_workers_widen_pool_and_report_truncation(monkeypatch) -> None:
    """workers>=2 honors the multiplier; truncated tail is made explicit."""
    capture: dict[str, object] = {"candidate_count": 30}
    _install_pipeline_stubs(monkeypatch, capture=capture)
    config = _base_config(
        daily_enrich_max_candidates=5,
        daily_fetch_max_workers=2,
    )

    result = screening_pipeline.screen("demo", use_llm=False, config=config)

    assert capture["enriched_rows"] == 15  # min(30, 5 * 3)
    assert (
        "Daily enrichment pool truncated: enriched=15 of 30 snapshot-filtered"
        in result.degradation
    )
    # Non-daily branch: the un-enriched tail never reaches ranking.
    assert "Non-daily candidates excluded from downstream ranking: 15" in result.degradation
    # Widening was active, so the serial-workers hint must stay absent.
    assert not any("pool widening inactive" in item for item in result.degradation)


def test_serial_workers_keep_legacy_cap_and_explain_inactive_widening(monkeypatch) -> None:
    capture: dict[str, object] = {"candidate_count": 30}
    _install_pipeline_stubs(monkeypatch, capture=capture)
    config = _base_config(
        daily_enrich_max_candidates=5,
        daily_fetch_max_workers=1,
    )

    result = screening_pipeline.screen("demo", use_llm=False, config=config)

    assert capture["enriched_rows"] == 5  # multiplier ignored while serial
    assert (
        "Daily enrichment pool truncated: enriched=5 of 30 snapshot-filtered"
        in result.degradation
    )
    assert (
        "DAILY_FETCH_MAX_WORKERS=1: pool widening inactive; "
        "set workers>=2 to use DAILY_ENRICH_POOL_MULTIPLIER"
    ) in result.degradation


def test_small_pool_reports_neither_truncation_nor_serial_hint(monkeypatch) -> None:
    capture: dict[str, object] = {"candidate_count": 4}
    _install_pipeline_stubs(monkeypatch, capture=capture)
    config = _base_config(
        daily_enrich_max_candidates=5,
        daily_fetch_max_workers=2,
    )

    result = screening_pipeline.screen("demo", use_llm=False, config=config)

    assert capture["enriched_rows"] == 4
    assert not any("pool truncated" in item for item in result.degradation)
    assert not any("pool widening inactive" in item for item in result.degradation)
    assert not any("excluded from downstream ranking" in item for item in result.degradation)


def test_risk_overlay_receives_below_cut_candidates_as_reserves(monkeypatch) -> None:
    capture: dict[str, object] = {"candidate_count": 6}
    _install_pipeline_stubs(monkeypatch, capture=capture)
    overlay_calls: dict[str, object] = {}

    def spy_overlay(picks, **kwargs):
        overlay_calls["shortlist_codes"] = [pick.code for pick in picks]
        overlay_calls.update(kwargs)
        return real_apply_risk_overlay(picks, **kwargs)

    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", spy_overlay)
    config = _base_config(
        daily_enrich_enabled=False,
        llm_candidate_multiplier=1,
    )

    result = screening_pipeline.screen(
        "demo", use_llm=False, config=config, max_output=1
    )

    # top_k = min(max(1 * 1, 1), 30, 6) = 1 shortlist slot.
    assert overlay_calls["shortlist_codes"] == ["600000"]
    reserves = overlay_calls["reserve_candidates"]
    assert [pick.code for pick in reserves] == [
        "600001",
        "600002",
        "600003",
        "600004",
        "600005",
    ]
    assert result.picks


def test_llm_prompt_cap_passed_to_ranker_when_configured(monkeypatch) -> None:
    capture: dict[str, object] = {"candidate_count": 3}
    _install_pipeline_stubs(monkeypatch, capture=capture)
    rank_calls: dict[str, object] = {}

    def fake_rank(candidates, *args, **kwargs):
        rank_calls.update(kwargs)
        return LLMRankingResult(picks=candidates, ranked=True, coverage=1.0)

    monkeypatch.setattr(screening_pipeline, "rank_candidates_with_metadata", fake_rank)
    config = _base_config(
        daily_enrich_enabled=False,
        risk_enabled=False,
        llm_api_key="test-key",
        llm_ranking_max_prompt_chars=12_000,
    )

    screening_pipeline.screen("demo", use_llm=True, config=config)

    assert rank_calls["max_prompt_chars"] == 12_000


def test_absent_prompt_cap_keeps_ranker_default_instead_of_unbounded(monkeypatch) -> None:
    """None must mean the ranker's built-in budget, not an unbounded prompt."""
    capture: dict[str, object] = {"candidate_count": 3}
    _install_pipeline_stubs(monkeypatch, capture=capture)
    rank_calls: dict[str, object] = {}

    def fake_rank(candidates, *args, **kwargs):
        rank_calls.update(kwargs)
        return LLMRankingResult(picks=candidates, ranked=True, coverage=1.0)

    monkeypatch.setattr(screening_pipeline, "rank_candidates_with_metadata", fake_rank)
    config = _base_config(
        daily_enrich_enabled=False,
        risk_enabled=False,
        llm_api_key="test-key",
        llm_ranking_max_prompt_chars=None,
    )

    screening_pipeline.screen("demo", use_llm=True, config=config)

    assert "max_prompt_chars" not in rank_calls


def test_pool_multiplier_env_parsing_and_clamping(monkeypatch) -> None:
    monkeypatch.delenv("DAILY_ENRICH_POOL_MULTIPLIER", raising=False)
    assert ScreeningRuntimeConfig.from_env().daily_enrich_pool_multiplier == 3

    monkeypatch.setenv("DAILY_ENRICH_POOL_MULTIPLIER", "7")
    assert ScreeningRuntimeConfig.from_env().daily_enrich_pool_multiplier == 7

    monkeypatch.setenv("DAILY_ENRICH_POOL_MULTIPLIER", "0")
    assert ScreeningRuntimeConfig.from_env().daily_enrich_pool_multiplier == 1

    monkeypatch.setenv("DAILY_ENRICH_POOL_MULTIPLIER", "-4")
    assert ScreeningRuntimeConfig.from_env().daily_enrich_pool_multiplier == 1


def test_prompt_cap_env_parsing(monkeypatch) -> None:
    monkeypatch.delenv("LLM_RANKING_MAX_PROMPT_CHARS", raising=False)
    assert ScreeningRuntimeConfig.from_env().llm_ranking_max_prompt_chars is None

    monkeypatch.setenv("LLM_RANKING_MAX_PROMPT_CHARS", "9000")
    assert ScreeningRuntimeConfig.from_env().llm_ranking_max_prompt_chars == 9000

    monkeypatch.setenv("LLM_RANKING_MAX_PROMPT_CHARS", "none")
    assert ScreeningRuntimeConfig.from_env().llm_ranking_max_prompt_chars is None


def test_rsi_all_rising_window_is_overbought() -> None:
    closes = pd.Series([10.0 + index for index in range(16)])
    assert _compute_rsi(closes) == 100.0


def test_rsi_all_flat_window_is_neutral() -> None:
    assert _compute_rsi(pd.Series([10.0] * 16)) == 50.0


def test_rsi_all_declining_window_is_oversold_zero() -> None:
    closes = pd.Series([20.0 - index for index in range(16)])
    assert _compute_rsi(closes) == 0.0


def test_rsi_mixed_series_matches_wilder_formula() -> None:
    closes = pd.Series(
        [
            10.0, 10.5, 10.2, 10.8, 11.0, 10.9, 11.3, 11.1,
            11.6, 11.4, 11.9, 12.1, 11.8, 12.3, 12.6, 12.2, 12.5,
        ]
    )
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    expected = 100 - (100 / (1 + float(gain.iloc[-1]) / float(loss.iloc[-1])))
    assert _compute_rsi(closes) == pytest.approx(expected)


def test_rsi_short_series_returns_none() -> None:
    assert _compute_rsi(pd.Series([10.0] * 14)) is None


def _history_frame(rows: int, *, close_only: bool) -> pd.DataFrame:
    data = {
        "date": pd.date_range("2026-01-01", periods=rows, freq="D"),
        "close": [10.0 + index * 0.05 for index in range(rows)],
        "volume": [1_000_000 + index for index in range(rows)],
    }
    if not close_only:
        data.update(
            {
                "open": [10.0 + index * 0.05 - 0.02 for index in range(rows)],
                "high": [10.0 + index * 0.05 + 0.05 for index in range(rows)],
                "low": [10.0 + index * 0.05 - 0.05 for index in range(rows)],
            }
        )
    return pd.DataFrame(data)


def test_close_only_history_publishes_synthetic_ratio_and_penalty() -> None:
    # Production passes the fetched frame itself; normalize stamps audit
    # attrs onto it so _compute_daily_quality(raw=hist, ...) can read them.
    hist = _history_frame(70, close_only=True)
    normalized = _normalize_daily_history(hist)

    assert normalized.attrs["synthetic_ohlc_filled_rows"] == 70
    assert normalized.attrs["synthetic_ohlc_ratio"] == pytest.approx(1.0)
    assert hist.attrs["synthetic_ohlc_ratio"] == pytest.approx(1.0)

    quality = _compute_daily_quality(hist, normalized)
    assert quality["daily_quality_score"] == pytest.approx(75.0)  # 100 - min(1.0*40, 25)
    assert "synthetic_ohlc" in quality["daily_quality_flags"].split(";")

    features = compute_daily_features(_history_frame(70, close_only=True))
    assert features["daily_quality_score"] < 100.0
    assert "synthetic_ohlc" in str(features["daily_quality_flags"]).split(";")


def test_full_ohlc_history_keeps_clean_quality() -> None:
    hist = _history_frame(70, close_only=False)
    normalized = _normalize_daily_history(hist)

    assert normalized.attrs["synthetic_ohlc_filled_rows"] == 0
    assert normalized.attrs["synthetic_ohlc_ratio"] == 0.0

    quality = _compute_daily_quality(hist, normalized)
    assert quality["daily_quality_score"] == pytest.approx(100.0)
    assert quality["daily_quality_flags"] == ""
