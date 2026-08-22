# -*- coding: utf-8 -*-
"""Regression contracts for the DSA-owned screening implementation."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import call, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient

from api.v1.router import router
from src.services.screening import REFERENCE_REVISION
from src.services.screening.dsa_provider import apply_dsa_provider_context
from src.services.screening import pipeline as screening_pipeline
from src.services.screening import post_analysis as screening_post_analysis
from src.services.screening.filter import apply_hard_filters
from src.services.screening.config import Config as ScreeningRuntimeConfig
from src.services.screening.models import HardFilterConfig, Pick, ScreeningConfig, Strategy
from src.services.screening.industry import load_industry_map
from src.services.screening.risk import apply_risk_overlay
from src.services.screening.scorer import compute_screen_scores
from src.services.screening import snapshot as screening_snapshot
from src.services.screening.strategy import list_strategies, load_all_strategies


REPO_ROOT = Path(__file__).resolve().parents[1]
SCREENING_ROOT = REPO_ROOT / "src" / "services" / "screening"


def test_screening_engine_is_collected_from_the_internal_package() -> None:
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "alphasift.git" not in requirements
    assert "#egg=alphasift" not in requirements
    assert "import src.services.screening.pipeline" in dockerfile


def test_screening_routes_have_a_primary_prefix_and_no_install_endpoint() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema_paths = app.openapi()["paths"]
    assert "/api/v1/screening/status" in schema_paths
    assert "/api/v1/alphasift/status" not in schema_paths

    client = FastAPITestClient(app)
    assert client.request("OPTIONS", "/api/v1/screening/status").status_code != 404
    assert client.request("OPTIONS", "/api/v1/alphasift/status").status_code == 404
    assert client.request("POST", "/api/v1/screening/install").status_code == 404


def test_bundled_engine_keeps_source_and_license_notices() -> None:
    notice = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    license_text = (SCREENING_ROOT / "LICENSE").read_text(encoding="utf-8")

    assert REFERENCE_REVISION in notice
    assert "Apache License" in license_text
    derived_files = [
        *SCREENING_ROOT.glob("*.py"),
        *(SCREENING_ROOT / "strategies").glob("*.yaml"),
    ]
    assert derived_files
    for path in derived_files:
        source = path.read_text(encoding="utf-8")
        assert f"Derived from AlphaSift revision {REFERENCE_REVISION}." in source


def test_bundled_strategies_are_loaded_from_the_internal_package() -> None:
    strategies = load_all_strategies(SCREENING_ROOT / "strategies")

    assert set(strategies) == {
        "balanced_alpha",
        "blue_chip_income",
        "capital_heat",
        "dual_low",
        "low_volatility_quality",
        "momentum_quality",
        "oversold_reversal",
        "quality_value",
        "shrink_pullback",
        "volume_breakout",
    }
    assert strategies["dual_low"].screening.factor_weights["value"] < 0.40


def test_list_strategies_preserves_legacy_strategies_dir_override() -> None:
    strategy_yaml = """
name: custom_demo
display_name: 自定义策略
description: custom strategy
screening:
  enabled: true
  market_scope: [cn]
  hard_filters: {}
  factor_weights:
    value: 1.0
  max_output: 3
""".strip()

    with tempfile.TemporaryDirectory() as temp_dir:
        strategy_path = Path(temp_dir) / "custom_demo.yaml"
        strategy_path.write_text(strategy_yaml, encoding="utf-8")

        with patch.dict(os.environ, {"STRATEGIES_DIR": temp_dir}, clear=False):
            config = ScreeningRuntimeConfig.from_env()
            strategies = list_strategies()

    assert config.strategies_dir == Path(temp_dir)
    assert [item.name for item in strategies] == ["custom_demo"]


def test_screening_config_reads_snapshot_cache_ttl() -> None:
    with patch.dict(
        os.environ,
        {"SCREENING_SNAPSHOT_CACHE_TTL_SEC": "120"},
        clear=False,
    ):
        config = ScreeningRuntimeConfig.from_env()

    assert config.snapshot_cache_ttl_seconds == 120.0


def test_pipeline_passes_daily_history_cache_settings_to_enrichment(monkeypatch) -> None:
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "Ping An",
                "price": 10.0,
                "change_pct": 1.2,
                "amount": 200_000_000.0,
            }
        ]
    )
    snapshot_df.attrs.update({
        "snapshot_source": "sina",
        "source_errors": [],
        "fallback_used": False,
    })
    strategy = Strategy(
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
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        daily_enrich_enabled=True,
        daily_history_cache_dir=Path("/tmp/daily-history-cache"),
        daily_history_cache_ttl_hours=6,
        post_analyzers=[],
        risk_enabled=False,
        portfolio_diversity_enabled=False,
    )
    captured: dict[str, object] = {}

    def fake_enrich_daily_features(df: pd.DataFrame, **kwargs):
        captured.update(kwargs)
        enriched = df.copy()
        enriched["daily_source"] = "cache"
        enriched.attrs["daily_errors"] = []
        enriched.attrs["daily_success_count"] = len(enriched)
        enriched.attrs["daily_source_counts"] = {"cache": len(enriched)}
        enriched.attrs["daily_quality_flag_counts"] = {}
        enriched.attrs["daily_source_order_notes"] = []
        enriched.attrs["daily_source_health"] = {}
        return enriched

    monkeypatch.setattr(screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: snapshot_df.copy())
    monkeypatch.setattr(screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy())
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=88.0),
    )
    monkeypatch.setattr(screening_pipeline, "enrich_daily_features", fake_enrich_daily_features)
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: [])
    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, []))

    history_fetcher = lambda *_args, **_kwargs: pd.DataFrame()
    result = screening_pipeline.screen(
        "demo",
        use_llm=False,
        config=config,
        daily_history_fetcher=history_fetcher,
    )

    assert captured["cache_dir"] == Path("/tmp/daily-history-cache")
    assert captured["cache_ttl_seconds"] == 6 * 60 * 60
    assert captured["history_fetcher"] is history_fetcher
    assert result.daily_enriched is True


def test_dsa_post_analyzer_records_attempted_and_capped_statuses(monkeypatch) -> None:
    picks = [
        Pick(
            rank=index,
            code=f"00000{index}",
            name=f"Stock {index}",
            final_score=90.0 - index,
            screen_score=90.0 - index,
        )
        for index in range(1, 5)
    ]
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        dsa_api_url="https://dsa.example",
    )

    def fake_analyze(candidates, **_kwargs):
        candidates[0].deep_analysis_status = "completed"
        candidates[0].deep_analysis_summary = "completed"
        candidates[1].deep_analysis_status = "failed"
        candidates[1].deep_analysis_summary = "failed"
        return candidates, ["one failure"]

    monkeypatch.setattr(screening_post_analysis, "analyze_picks_with_dsa", fake_analyze)
    # A completed remote overlay can move an attempted pick below an
    # unattempted one. Status ownership must follow the original attempted
    # objects, not the post-overlay list positions.
    monkeypatch.setattr(
        screening_post_analysis,
        "apply_dsa_overlay",
        lambda candidates: [candidates[2], candidates[0], candidates[3], candidates[1]],
    )

    analyzed, degradation = screening_post_analysis.run_post_analyzers(
        picks,
        analyzer_names=["dsa"],
        run_id="run-1",
        config=config,
        max_picks=2,
    )

    status_by_code = {
        pick.code: pick.post_analysis_status.get("dsa")
        for pick in analyzed
    }
    assert status_by_code == {
        "000001": "completed",
        "000002": "failed",
        "000003": "skipped",
        "000004": "skipped",
    }
    assert degradation == ["one failure"]


def test_scorecard_reranks_before_capped_dsa_analyzer(monkeypatch) -> None:
    picks = [
        Pick(
            rank=index,
            code=f"00000{index}",
            name=f"Stock {index}",
            final_score=91.0 - index,
            screen_score=91.0 - index,
            factor_scores={"value": 50.0, "stability": 50.0},
        )
        for index in range(1, 5)
    ]
    # The original fourth candidate crosses the remote Top-3 cutoff after the
    # full-pool scorecard applies its value/quality bonus.
    picks[3].factor_scores = {"value": 100.0, "stability": 100.0}
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        dsa_api_url="https://dsa.example",
    )
    attempted_codes: list[str] = []

    def fake_analyze(candidates, *, max_picks, **_kwargs):
        attempted = candidates[:max_picks]
        attempted_codes.extend(pick.code for pick in attempted)
        for pick in attempted:
            pick.deep_analysis_status = "completed"
        return candidates, []

    monkeypatch.setattr(screening_post_analysis, "analyze_picks_with_dsa", fake_analyze)
    monkeypatch.setattr(screening_post_analysis, "apply_dsa_overlay", lambda candidates: candidates)

    analyzed, degradation = screening_post_analysis.run_post_analyzers(
        picks,
        analyzer_names=["scorecard", "dsa"],
        run_id="run-1",
        config=config,
        max_picks=3,
    )

    assert attempted_codes == ["000001", "000004", "000002"]
    assert {
        pick.code: pick.post_analysis_status.get("dsa")
        for pick in analyzed
    } == {
        "000004": "completed",
        "000001": "completed",
        "000002": "completed",
        "000003": "skipped",
    }
    assert degradation == []


def test_external_post_analyzer_rejects_results_beyond_remote_cap(monkeypatch) -> None:
    picks = [
        Pick(
            rank=index,
            code=f"00000{index}",
            name=f"Stock {index}",
            final_score=90.0 - index,
            screen_score=90.0 - index,
        )
        for index in range(1, 4)
    ]
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        post_analyzer_url="https://analyzer.example/rank",
    )
    captured = {}

    def fake_post(_url, *, json, timeout):
        captured["payload"] = json
        captured["timeout"] = timeout
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [
                {"code": "000001", "score_delta": 1.0, "risk_flags": ["submitted"]},
                {"code": "000001", "score_delta": 50.0, "risk_flags": ["duplicate"]},
                {"code": "000003", "score_delta": 50.0, "risk_flags": ["not-submitted"]},
            ],
        )

    monkeypatch.setattr(screening_post_analysis.requests, "post", fake_post)

    analyzed, degradation = screening_post_analysis.run_post_analyzers(
        picks,
        analyzer_names=["external_http"],
        run_id="run-1",
        config=config,
        max_picks=2,
    )

    by_code = {pick.code: pick for pick in analyzed}
    assert [item["code"] for item in captured["payload"]["candidates"]] == ["000001", "000002"]
    assert by_code["000001"].final_score == 90.0
    assert by_code["000001"].risk_flags == ["submitted"]
    assert by_code["000001"].post_analysis_status["external_http"] == "completed"
    assert by_code["000002"].post_analysis_status["external_http"] == "failed"
    assert by_code["000003"].final_score == 87.0
    assert by_code["000003"].risk_flags == []
    assert by_code["000003"].post_analysis_status["external_http"] == "skipped"
    assert degradation == []


def test_pipeline_uses_ranker_success_flag_instead_of_partial_llm_scores(monkeypatch) -> None:
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "Ping An",
                "price": 10.0,
                "change_pct": 1.2,
                "amount": 200_000_000.0,
            }
        ]
    )
    snapshot_df.attrs.update({
        "snapshot_source": "sina",
        "source_errors": [],
        "fallback_used": False,
    })
    strategy = Strategy(
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
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        llm_model="openai/gpt-5-mini",
        llm_api_key="test-key",
        post_analyzers=[],
        risk_enabled=False,
        portfolio_diversity_enabled=False,
    )

    def fake_ranker(picks, *_args, **_kwargs):
        leaked_pick = picks[0]
        leaked_pick.llm_score = 99.0
        return SimpleNamespace(
            picks=picks,
            ranked=False,
            market_view="",
            selection_logic="",
            portfolio_risk="",
            coverage=0.5,
            errors=["coverage below threshold"],
        )

    monkeypatch.setattr(screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: snapshot_df.copy())
    monkeypatch.setattr(screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy())
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=88.0),
    )
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: [])
    monkeypatch.setattr(screening_pipeline, "rank_candidates_with_metadata", fake_ranker)
    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, []))

    result = screening_pipeline.screen("demo", use_llm=True, config=config)

    assert result.llm_ranked is False
    assert result.picks[0].llm_score is None
    assert result.picks[0].final_score == result.picks[0].screen_score
    assert "LLM ranking failed: fell back to screen_score" in result.degradation


def test_default_scorecard_scores_full_pool_before_deterministic_seeded_rotation(monkeypatch) -> None:
    snapshot_df = pd.DataFrame([
        {
            "code": f"00000{index}",
            "name": f"Stock {index}",
            "price": 10.0,
            "change_pct": 1.0,
            "amount": 200_000_000.0,
            "raw_score": score,
        }
        for index, score in enumerate([84.2, 84.1, 84.0, 83.9, 83.8], start=1)
    ])
    snapshot_df.attrs.update({
        "snapshot_source": "sina",
        "source_errors": [],
        "fallback_used": False,
    })
    strategy = Strategy(
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
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        post_analyzers=["scorecard"],
        post_analysis_max_picks=3,
        llm_candidate_multiplier=2,
        llm_max_candidates=10,
        risk_enabled=False,
        portfolio_diversity_enabled=False,
    )
    observed_statuses: list[list[str | None]] = []
    original_variant = screening_pipeline.apply_seeded_selection_variant

    def capture_variant(picks, **kwargs):
        observed_statuses.append([
            pick.post_analysis_status.get("scorecard") for pick in picks
        ])
        return original_variant(picks, **kwargs)

    run_ids = iter(f"{index:012d}" for index in range(20))
    monkeypatch.setattr(screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: snapshot_df.copy())
    monkeypatch.setattr(screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy())
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=df["raw_score"]),
    )
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: [])
    monkeypatch.setattr(screening_pipeline, "apply_seeded_selection_variant", capture_variant)
    monkeypatch.setattr(screening_pipeline.uuid, "uuid4", lambda: SimpleNamespace(hex=next(run_ids)))

    variants = {
        tuple(
            pick.code
            for pick in screening_pipeline.screen(
                "demo",
                max_output=3,
                use_llm=False,
                selection_seed="browser-a",
                config=config,
            ).picks
        )
        for _ in range(20)
    }

    assert all(statuses == ["completed"] * 5 for statuses in observed_statuses)
    # period 已剔除随机 run_id：同一 client seed + 同一交易日 + 同一候选池
    # 的 rotation 必须完全可复现（每日轮换由 period 的日期分量承担）。
    assert len(variants) == 1


def test_dsa_provider_context_respects_host_max_candidates_setting() -> None:
    picks = [
        Pick(rank=index + 1, code=f"00000{index + 1}", name=f"Stock {index + 1}", final_score=90.0, screen_score=90.0)
        for index in range(5)
    ]
    requested_codes: list[str] = []

    def get_candidate_context(code: str, _name: str) -> dict[str, object]:
        requested_codes.append(code)
        return {"enriched": True, "quote": {"price": 10.0}}

    notes = apply_dsa_provider_context(
        picks,
        {"dsa": {"max_candidates": 3, "get_candidate_context": get_candidate_context}},
    )

    assert requested_codes == ["000001", "000002", "000003"]
    assert all(pick.dsa_context.get("enriched") is True for pick in picks[:3])
    assert all(pick.dsa_context == {} for pick in picks[3:])
    assert notes == ["DSA provider context applied 3 of 3 candidates"]


def test_hard_filter_and_factor_scoring_keep_core_semantics() -> None:
    frame = pd.DataFrame(
        [
            {
                "code": "low_value",
                "name": "Low Value",
                "price": 10.0,
                "amount": 200_000_000,
                "pe_ratio": 5.0,
                "pb_ratio": 0.6,
                "turnover_rate": 2.0,
                "volume_ratio": 1.2,
                "change_pct": 0.0,
            },
            {
                "code": "high_value",
                "name": "High Value",
                "price": 10.0,
                "amount": 20_000_000,
                "pe_ratio": 15.0,
                "pb_ratio": 2.0,
                "turnover_rate": 2.0,
                "volume_ratio": 1.2,
                "change_pct": 0.0,
            },
        ]
    )

    filtered = apply_hard_filters(frame, HardFilterConfig(amount_min=100_000_000))
    assert filtered["code"].tolist() == ["low_value"]

    scored = compute_screen_scores(
        frame,
        ScreeningConfig(factor_weights={"value": 1.0}),
    ).set_index("code")
    assert scored.loc["low_value", "screen_score"] > scored.loc["high_value", "screen_score"]


def test_snapshot_schema_mismatch_counts_toward_source_circuit_breaker(
    monkeypatch,
) -> None:
    bad_snapshot = pd.DataFrame([{"code": "000001", "name": "Ping An", "price": 10.0}])
    good_snapshot = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    source_health: dict[str, dict[str, object]] = {}
    calls: list[str] = []

    def fake_fetch(source: str) -> pd.DataFrame:
        calls.append(source)
        if source == "sina":
            return bad_snapshot
        if source == "efinance":
            return good_snapshot.copy()
        raise AssertionError(f"unexpected source {source}")

    monkeypatch.setattr(screening_snapshot, "_SOURCE_HEALTH", source_health)
    monkeypatch.setattr(screening_snapshot, "fetch_cn_snapshot", fake_fetch)

    for _ in range(3):
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["sina", "efinance"],
            required_columns=["volume_ratio"],
        )
        assert result.attrs["snapshot_source"] == "efinance"

    health = screening_snapshot.snapshot_source_health_snapshot(["sina"])
    assert health["sina"]["failures"] == 3
    assert health["sina"]["disabled"] is True

    calls.clear()
    result = screening_snapshot.fetch_snapshot_with_fallback(
        ["sina", "efinance"],
        required_columns=["volume_ratio"],
    )

    assert calls == ["efinance"]
    assert result.attrs["snapshot_source"] == "efinance"
    assert "temporarily disabled" in result.attrs["source_errors"][0]


def test_sina_snapshot_uses_timeout_wrapper(monkeypatch) -> None:
    expected = pd.DataFrame([{"code": "000001"}])
    captured: dict[str, object] = {}

    def fake_wrapper(fetcher, *, source: str) -> pd.DataFrame:
        captured["fetcher"] = fetcher
        captured["source"] = source
        return expected

    monkeypatch.setattr(screening_snapshot, "_call_snapshot_wrapper", fake_wrapper)
    monkeypatch.setattr(screening_snapshot, "_fetch_sina", lambda: expected)

    result = screening_snapshot.fetch_cn_snapshot("sina")

    assert result is expected
    assert captured["source"] == "sina"
    assert captured["fetcher"] is screening_snapshot._fetch_sina


def test_em_datacenter_snapshot_uses_timeout_wrapper(monkeypatch) -> None:
    expected = pd.DataFrame([{"code": "000001"}])
    captured: dict[str, object] = {}

    def fake_wrapper(fetcher, *, source: str) -> pd.DataFrame:
        captured["fetcher"] = fetcher
        captured["source"] = source
        return expected

    monkeypatch.setattr(screening_snapshot, "_call_snapshot_wrapper", fake_wrapper)
    monkeypatch.setattr(screening_snapshot, "_fetch_em_datacenter", lambda: expected)

    result = screening_snapshot.fetch_cn_snapshot("em_datacenter")

    assert result is expected
    assert captured["source"] == "em_datacenter"
    assert captured["fetcher"] is screening_snapshot._fetch_em_datacenter


def test_em_datacenter_timeout_falls_back_to_last_good_snapshot(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "cached:last_good"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)

    def fake_wrapper(fetcher, *, source: str) -> pd.DataFrame:
        assert source == "em_datacenter"
        assert fetcher is screening_snapshot._fetch_em_datacenter
        raise RuntimeError("snapshot source em_datacenter timed out")

    monkeypatch.setattr(screening_snapshot, "_call_snapshot_wrapper", fake_wrapper)

    result = screening_snapshot.fetch_snapshot_with_fallback(
        ["em_datacenter"],
        required_columns=["volume_ratio"],
        fallback_snapshot_path=cache_path,
    )

    assert result.attrs["fallback_used"] is True
    assert result.attrs["snapshot_source"] == "last_good_cache"
    assert result.loc[0, "code"] == "000001"


def test_fresh_snapshot_cache_skips_live_sources(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)
    live_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        side_effect=AssertionError("fresh cache should avoid live snapshot calls"),
    )

    with live_fetch as fetch_mock:
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_not_called()
    assert result.attrs["snapshot_source"] == "last_good_cache"
    assert result.attrs["cache_used"] is True
    assert result.attrs["fallback_used"] is False
    assert result.attrs["stale"] is False
    assert result.attrs["last_good_snapshot_source"] == "sina"


def test_fresh_snapshot_cache_ignores_mismatched_source(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)

    live = pd.DataFrame(
        [{"code": "000002", "name": "Vanke", "price": 11.0, "volume_ratio": 2.0}]
    )
    live.attrs["snapshot_source"] = "tushare"
    live_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        autospec=True,
        return_value=live,
    )

    with live_fetch as fetch_mock:
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_called_once_with("tushare")
    assert result.loc[0, "code"] == "000002"
    assert result.attrs["snapshot_source"] == "tushare"
    assert result.attrs["fallback_used"] is False
    assert result.attrs["stale"] is False


def test_fresh_snapshot_cache_ignores_non_primary_source(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)

    live = pd.DataFrame(
        [{"code": "000002", "name": "Vanke", "price": 11.0, "volume_ratio": 2.0}]
    )
    live.attrs["snapshot_source"] = "tushare"
    live_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        autospec=True,
        return_value=live,
    )

    with live_fetch as fetch_mock:
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare", "sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_called_once_with("tushare")
    assert result.loc[0, "code"] == "000002"
    assert result.attrs["snapshot_source"] == "tushare"
    assert result.attrs["fallback_used"] is False
    assert result.attrs["stale"] is False


def test_fresh_snapshot_cache_reuses_fallback_from_same_source_chain(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "snapshot-cache.json"
    live = pd.DataFrame(
        [{"code": "000002", "name": "Vanke", "price": 11.0, "volume_ratio": 2.0}]
    )
    live.attrs["snapshot_source"] = "sina"

    first_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        autospec=True,
        side_effect=[RuntimeError("tushare unavailable"), live],
    )
    with first_fetch as fetch_mock:
        first = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare", "sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    assert fetch_mock.call_args_list == [call("tushare"), call("sina")]
    assert first.attrs["snapshot_source"] == "sina"

    second_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        side_effect=AssertionError("same-chain fresh cache should avoid live calls"),
    )
    with second_fetch as fetch_mock:
        second = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare", "sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_not_called()
    assert second.attrs["snapshot_source"] == "last_good_cache"
    assert second.attrs["last_good_snapshot_source"] == "sina"
    assert second.attrs["fallback_used"] is False


class _FakeTusharePro:
    """Minimal Tushare Pro stub returning per-date EOD tables."""

    def __init__(self, *, open_dates, daily_by_date=None, daily_basic_by_date=None):
        self._open_dates = list(open_dates)
        self._daily_by_date = dict(daily_by_date or {})
        self._daily_basic_by_date = dict(daily_basic_by_date or {})
        self.daily_calls: list[str] = []
        self.daily_basic_calls: list[str] = []
        self.stock_basic_calls = 0

    def trade_cal(self, **kwargs):
        return pd.DataFrame(
            {"cal_date": list(self._open_dates), "is_open": ["1"] * len(self._open_dates)}
        )

    def daily(self, trade_date=None, fields=None):
        self.daily_calls.append(str(trade_date))
        return self._daily_by_date.get(str(trade_date), pd.DataFrame())

    def daily_basic(self, trade_date=None, fields=None):
        self.daily_basic_calls.append(str(trade_date))
        return self._daily_basic_by_date.get(str(trade_date), pd.DataFrame())

    def stock_basic(self, **kwargs):
        self.stock_basic_calls += 1
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "symbol": ["000001"],
                "name": ["Ping An"],
                "industry": ["bank"],
            }
        )


def _tushare_daily_rows(trade_date: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "close": 10.0,
                "pct_chg": 1.0,
                "amount": 1000.0,
            }
        ]
    )


def _tushare_daily_basic_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "turnover_rate": 1.5,
                "volume_ratio": 1.2,
                "pe": 9.0,
                "pb": 0.9,
                "total_mv": 100.0,
                "circ_mv": 80.0,
            }
        ]
    )


def _patch_tushare_pro(monkeypatch, pro: _FakeTusharePro) -> None:
    import tushare as ts

    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.delenv("TUSHARE_TRADE_DATE", raising=False)
    monkeypatch.setattr(ts, "pro_api", lambda token: pro)


def test_tushare_snapshot_walks_back_to_previous_open_day_when_latest_daily_empty(monkeypatch) -> None:
    pro = _FakeTusharePro(
        open_dates=["20260817", "20260818", "20260819"],
        daily_by_date={"20260818": _tushare_daily_rows("20260818")},
        daily_basic_by_date={"20260818": _tushare_daily_basic_rows()},
    )
    _patch_tushare_pro(monkeypatch, pro)

    result = screening_snapshot._fetch_tushare()

    assert pro.daily_calls == ["20260819", "20260818"]
    assert pro.daily_basic_calls == ["20260818"]
    assert pro.stock_basic_calls == 1
    assert result.attrs["trade_date"] == "20260818"
    assert result.loc[0, "code"] == "000001"


def test_tushare_snapshot_walks_back_when_daily_basic_not_published(monkeypatch) -> None:
    pro = _FakeTusharePro(
        open_dates=["20260817", "20260818", "20260819"],
        daily_by_date={
            "20260819": _tushare_daily_rows("20260819"),
            "20260818": _tushare_daily_rows("20260818"),
        },
        daily_basic_by_date={"20260818": _tushare_daily_basic_rows()},
    )
    _patch_tushare_pro(monkeypatch, pro)

    result = screening_snapshot._fetch_tushare()

    assert pro.daily_calls == ["20260819", "20260818"]
    assert pro.daily_basic_calls == ["20260819", "20260818"]
    assert result.attrs["trade_date"] == "20260818"


def test_tushare_snapshot_raises_after_exhausting_open_day_lookback(monkeypatch) -> None:
    pro = _FakeTusharePro(
        open_dates=[
            "20260812",
            "20260813",
            "20260814",
            "20260817",
            "20260818",
            "20260819",
            "20260820",
        ],
    )
    _patch_tushare_pro(monkeypatch, pro)

    with pytest.raises(RuntimeError) as excinfo:
        screening_snapshot._fetch_tushare()

    assert pro.daily_calls == ["20260820", "20260819", "20260818", "20260817", "20260814"]
    assert pro.daily_basic_calls == []
    assert pro.stock_basic_calls == 0
    message = str(excinfo.value)
    assert "tushare daily returned empty data for 20260820" in message
    assert "looked back over 5 open days" in message


def test_tushare_snapshot_explicit_trade_date_does_not_walk_back(monkeypatch) -> None:
    pro = _FakeTusharePro(
        open_dates=["20260818", "20260819"],
        daily_by_date={"20260818": _tushare_daily_rows("20260818")},
        daily_basic_by_date={"20260818": _tushare_daily_basic_rows()},
    )
    _patch_tushare_pro(monkeypatch, pro)
    monkeypatch.setenv("TUSHARE_TRADE_DATE", "20260819")

    with pytest.raises(RuntimeError) as excinfo:
        screening_snapshot._fetch_tushare()

    assert pro.daily_calls == ["20260819"]
    assert pro.stock_basic_calls == 0
    message = str(excinfo.value)
    assert "tushare daily returned empty data for 20260819" in message
    assert "looked back" not in message


def _risk_pick(code: str, final_score: float, *, risky: bool) -> Pick:
    """Deterministic pick: risky=True accumulates 13 points ('high'); else 0 ('low')."""
    return Pick(
        rank=0,
        code=code,
        name=f"stock-{code}",
        final_score=final_score,
        screen_score=final_score,
        change_pct=9.0 if risky else 0.0,
        volume_ratio=7.0 if risky else None,
        turnover_rate=16.0 if risky else None,
        pe_ratio=-4.0 if risky else None,
    )


def test_load_industry_map_ignores_generic_change_and_rank_columns(tmp_path) -> None:
    map_path = tmp_path / "industry-map.json"
    map_path.write_text(
        json.dumps(
            [
                {"code": "000001", "industry": "银行", "涨跌幅": 3.21, "排名": 5},
                {"code": "600000", "industry": "地产", "行业涨跌幅": -1.5, "板块排名": 2},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mapping = load_industry_map(map_path)

    generic = mapping["000001"]
    assert generic["industry"] == "银行"
    assert "industry_change_pct" not in generic
    assert "industry_rank" not in generic

    scoped = mapping["600000"]
    assert scoped["industry_change_pct"] == pytest.approx(-1.5)
    assert scoped["industry_rank"] == 2


def test_risk_veto_backfills_from_reserves_to_original_shortlist_size() -> None:
    shortlist = [
        _risk_pick("600001", 90.0, risky=False),
        _risk_pick("600002", 95.0, risky=True),
        _risk_pick("600003", 85.0, risky=False),
    ]
    reserves = [
        _risk_pick("600101", 80.0, risky=True),
        _risk_pick("600102", 78.0, risky=False),
        _risk_pick("600103", 70.0, risky=False),
    ]

    kept, degradation = apply_risk_overlay(
        shortlist,
        veto_high_risk=True,
        reserve_candidates=reserves,
    )

    assert [pick.code for pick in kept] == ["600001", "600003", "600102"]
    assert [pick.rank for pick in kept] == [1, 2, 3]
    vetoed = next(pick for pick in shortlist if pick.code == "600002")
    assert vetoed.excluded_by_risk is True
    skipped_reserve = next(pick for pick in reserves if pick.code == "600101")
    assert skipped_reserve.excluded_by_risk is True
    backfilled = next(pick for pick in reserves if pick.code == "600102")
    assert backfilled.excluded_by_risk is False
    assert backfilled.risk_level == "low"
    unused_reserve = next(pick for pick in reserves if pick.code == "600103")
    assert unused_reserve.risk_level == ""
    assert degradation[-1] == "Risk veto backfilled=1"


def test_risk_veto_without_reserves_keeps_reduced_list() -> None:
    shortlist = [
        _risk_pick("600001", 90.0, risky=False),
        _risk_pick("600002", 95.0, risky=True),
        _risk_pick("600003", 85.0, risky=False),
    ]

    kept, degradation = apply_risk_overlay(shortlist, veto_high_risk=True)

    assert [pick.code for pick in kept] == ["600001", "600003"]
    assert not any("backfilled" in note for note in degradation)


def test_risk_overlay_ignores_reserves_when_veto_disabled() -> None:
    shortlist = [
        _risk_pick("600001", 90.0, risky=False),
        _risk_pick("600002", 95.0, risky=True),
    ]
    reserves = [_risk_pick("600101", 80.0, risky=False)]

    kept, degradation = apply_risk_overlay(shortlist, reserve_candidates=reserves)

    assert [pick.code for pick in kept] == ["600001", "600002"]
    assert reserves[0].risk_level == ""
    assert reserves[0].rank == 0
    assert degradation == []


def _write_stale_last_good_cache(path: Path, *, age_hours: float) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    screening_snapshot._write_last_good_snapshot(path, cached)
    payload = json.loads(path.read_text(encoding="utf-8"))
    created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    payload["created_at"] = created_at.isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_stale_fallback_snapshot_rejected_under_env_age_limit(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "snapshot-cache.json"
    _write_stale_last_good_cache(cache_path, age_hours=48.0)
    monkeypatch.setenv("DSA_SNAPSHOT_FALLBACK_MAX_AGE_HOURS", "24")

    errors: list[str] = []
    result = screening_snapshot._read_last_good_snapshot(
        cache_path,
        required_columns=["volume_ratio"],
        source_errors=errors,
    )

    assert result is None
    assert errors and errors[0].startswith(
        "last_good_cache: Stale fallback snapshot rejected: age="
    )
    assert errors[0].endswith("> limit=24.0h")


def test_stale_fallback_snapshot_allowed_without_env_limit(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "snapshot-cache.json"
    _write_stale_last_good_cache(cache_path, age_hours=48.0)
    monkeypatch.delenv("DSA_SNAPSHOT_FALLBACK_MAX_AGE_HOURS", raising=False)

    result = screening_snapshot._read_last_good_snapshot(
        cache_path,
        required_columns=["volume_ratio"],
        source_errors=[],
    )

    assert result is not None
    assert result.attrs["snapshot_source"] == "last_good_cache"
    assert result.attrs["stale"] is True


def test_env_age_limit_does_not_block_fresh_cache_reads(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "snapshot-cache.json"
    _write_stale_last_good_cache(cache_path, age_hours=48.0)
    monkeypatch.setenv("DSA_SNAPSHOT_FALLBACK_MAX_AGE_HOURS", "1")

    result = screening_snapshot._read_last_good_snapshot(
        cache_path,
        required_columns=["volume_ratio"],
        source_errors=[],
        fresh=True,
        requested_snapshot_sources=["sina"],
    )

    assert result is not None
    assert result.attrs["stale"] is False
