# -*- coding: utf-8 -*-
"""Wiring tests for the SkillOpinion outcome run endpoint and scheduled auto-run."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
import src.services.skill_opinion_outcome_service as skill_opinion_service_module
from api.app import create_app
from api.v1.endpoints import skill_opinions as skill_opinions_endpoint
from src.config import Config
from src.services import runtime_scheduler
from src.services.runtime_scheduler import (
    SKILL_OPINION_OUTCOME_AUTO_RUN_ENV,
    RuntimeSchedulerService,
    _is_skill_opinion_outcome_auto_run_enabled,
    run_skill_opinion_outcome_task,
)
from src.storage import DatabaseManager


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _outcome_run_result() -> Dict[str, Any]:
    return {
        "items": [
            {
                "id": 1,
                "skill_opinion_sample_id": 7,
                "analysis_history_id": 42,
                "stock_code": "600519",
                "skill_id": "trend_follow",
                "signal": "buy",
                "horizon": "3d",
                "engine_version": "skill-opinion-outcome-v1",
                "eval_status": "evaluated",
                "outcome": "hit",
                "direction_correct": True,
                "unable_reason": None,
                "analysis_date": "2026-08-20",
                "start_trade_date": "2026-08-21",
                "end_trade_date": "2026-08-25",
                "start_price": 10.0,
                "end_close": 10.5,
                "stock_return_pct": 5.0,
                "directional_return_pct": 5.0,
                "created_at": "2026-08-23T00:00:00",
                "updated_at": "2026-08-23T00:00:00",
            }
        ],
        "processed_keys": 2,
        "created": 1,
        "updated": 1,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "limit_unit": "outcome_key",
        "engine_version": "skill-opinion-outcome-v1",
    }


class _StubEndpointService:
    """Stub replacing SkillOpinionOutcomeService inside the endpoint module."""

    instances: List["_StubEndpointService"] = []

    def __init__(self) -> None:
        type(self).instances.append(self)
        self.received: Optional[Dict[str, Any]] = None
        self.result: Dict[str, Any] = {}
        self.error: Optional[Exception] = None

    def run_outcomes(self, **kwargs: Any) -> Dict[str, Any]:
        self.received = kwargs
        if self.error is not None:
            raise self.error
        return dict(self.result)


@pytest.fixture()
def endpoint_stub(monkeypatch):
    _StubEndpointService.instances = []
    stub_config: Dict[str, Any] = {"result": _outcome_run_result(), "error": None}

    def factory() -> _StubEndpointService:
        service = _StubEndpointService()
        service.result = stub_config["result"]
        service.error = stub_config["error"]
        return service

    monkeypatch.setattr(skill_opinions_endpoint, "SkillOpinionOutcomeService", factory)
    return stub_config


@pytest.fixture()
def client(tmp_path):
    old_env_file = os.environ.get("ENV_FILE")
    old_database_path = os.environ.get("DATABASE_PATH")
    env_path = tmp_path / ".env"
    db_path = tmp_path / "skill_opinion_api.db"
    static_dir = tmp_path / "empty-static"
    static_dir.mkdir()
    env_path.write_text(
        "\n".join(
            [
                "STOCK_LIST=600519",
                "GEMINI_API_KEY=test",
                "ADMIN_AUTH_ENABLED=false",
                f"DATABASE_PATH={db_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)

    def _reset_auth_globals() -> None:
        auth._auth_enabled = None
        auth._session_secret = None
        auth._password_hash_salt = None
        auth._password_hash_stored = None
        auth._rate_limit = {}

    _reset_auth_globals()
    Config.reset_instance()
    DatabaseManager.reset_instance()
    try:
        yield TestClient(create_app(static_dir=Path(static_dir)))
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        _reset_auth_globals()
        if old_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = old_env_file
        if old_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_database_path


RUN_URL = "/api/v1/skill-opinions/outcomes/run"


class TestSkillOpinionOutcomeRunEndpoint:
    def test_run_forwards_filters_and_returns_counts(self, client, endpoint_stub) -> None:
        endpoint_stub["result"] = _outcome_run_result()
        payload = {
            "sample_id": 7,
            "analysis_history_id": 42,
            "skill_id": "trend_follow",
            "stock_code": "600519",
            "horizons": ["3d"],
            "limit": 50,
        }

        resp = client.post(RUN_URL, json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["processed_keys"] == 2
        assert body["created"] == 1
        assert body["updated"] == 1
        assert body["skipped"] == 0
        assert body["failed"] == 0
        assert body["errors"] == []
        assert body["limit_unit"] == "outcome_key"
        assert body["engine_version"] == "skill-opinion-outcome-v1"
        assert len(body["items"]) == 1
        assert body["items"][0]["skill_opinion_sample_id"] == 7
        assert body["items"][0]["outcome"] == "hit"

        service = _StubEndpointService.instances[-1]
        assert service.received == {
            "sample_id": 7,
            "analysis_history_id": 42,
            "skill_id": "trend_follow",
            "stock_code": "600519",
            "horizons": ["3d"],
            "limit": 50,
        }

    def test_run_with_empty_body_uses_defaults(self, client, endpoint_stub) -> None:
        endpoint_stub["result"] = _outcome_run_result()

        resp = client.post(RUN_URL, json={})

        assert resp.status_code == 200
        service = _StubEndpointService.instances[-1]
        assert service.received == {
            "sample_id": None,
            "analysis_history_id": None,
            "skill_id": None,
            "stock_code": None,
            "horizons": None,
            "limit": 100,
        }

    def test_run_maps_value_error_to_400(self, client, endpoint_stub) -> None:
        endpoint_stub["error"] = ValueError("limit must be a positive integer no greater than 500")

        resp = client.post(RUN_URL, json={})

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "validation_error"
        assert "positive integer" in body["message"]

    def test_run_maps_unexpected_error_to_500(self, client, endpoint_stub) -> None:
        endpoint_stub["error"] = RuntimeError("database exploded")
        resp = client.post(RUN_URL, json={})

        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        assert body["message"] == "Run skill opinion outcomes failed"

    @pytest.mark.parametrize(
        "payload",
        [
            {"limit": 0},
            {"limit": 501},
            {"sample_id": 0},
            {"skill_id": ""},
            {"stock_code": "60051960051960051"},
        ],
    )
    def test_run_rejects_invalid_request_bodies(self, client, endpoint_stub, payload) -> None:
        resp = client.post(RUN_URL, json=payload)

        assert resp.status_code == 422
        assert not _StubEndpointService.instances


# ---------------------------------------------------------------------------
# Scheduled auto-run task
# ---------------------------------------------------------------------------


def _install_service_module_stub(monkeypatch, *, result=None, error=None):
    init_calls: List[bool] = []
    run_calls: List[Dict[str, Any]] = []

    class _StubScheduledService:
        def __init__(self) -> None:
            init_calls.append(True)

        def run_outcomes(self, **kwargs: Any) -> Dict[str, Any]:
            run_calls.append(kwargs)
            if error is not None:
                raise error
            return dict(result or {})
    monkeypatch.setattr(
        skill_opinion_service_module,
        "SkillOpinionOutcomeService",
        _StubScheduledService,
    )
    return init_calls, run_calls


class TestSkillOpinionAutoRunGate:
    @pytest.mark.parametrize("value", [None, "", "1", "true", "TRUE", "yes"])
    def test_enabled_by_default_or_truthy(self, monkeypatch, value) -> None:
        if value is None:
            monkeypatch.delenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, raising=False)
        else:
            monkeypatch.setenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, value)
        assert _is_skill_opinion_outcome_auto_run_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
    def test_disabled_by_explicit_falsy(self, monkeypatch, value) -> None:
        monkeypatch.setenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, value)
        assert _is_skill_opinion_outcome_auto_run_enabled() is False


    def test_returns_result_and_defaults_to_limit_100(self, monkeypatch) -> None:
        monkeypatch.delenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, raising=False)
        expected = _outcome_run_result()
        init_calls, run_calls = _install_service_module_stub(monkeypatch, result=expected)

        result = run_skill_opinion_outcome_task()

        assert result == expected
        assert init_calls == [True]
        assert run_calls == [{"limit": 100}]

    def test_noops_when_env_disables_auto_run(self, monkeypatch) -> None:
        monkeypatch.setenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, "0")
        init_calls, run_calls = _install_service_module_stub(
            monkeypatch, result=_outcome_run_result()
        )

        result = run_skill_opinion_outcome_task()

        assert result is None
        assert init_calls == []
        assert run_calls == []

    def test_swallows_service_exception(self, monkeypatch) -> None:
        monkeypatch.delenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, raising=False)
        _install_service_module_stub(monkeypatch, error=RuntimeError("db unavailable"))

        result = run_skill_opinion_outcome_task()  # must not raise

        assert result is None


# ---------------------------------------------------------------------------
# Daily scheduler attachment
# ---------------------------------------------------------------------------


class _NoopThread:
    def __init__(self, target=None, **kwargs):
        self.target = target

    def start(self):
        pass

    def is_alive(self):
        return False


class _FakeScheduler:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.background_tasks: List[Dict[str, Any]] = []
        self.daily_task = None
        self.daily_task_run_immediately = None

    def set_daily_task(self, task, run_immediately: bool) -> None:
        self.daily_task = task
        self.daily_task_run_immediately = run_immediately

    def add_background_task(
        self,
        task,
        interval_seconds: int,
        run_immediately: bool,
        name: Optional[str] = None,
    ) -> None:
        self.background_tasks.append(
            {
                "task": task,
                "interval_seconds": interval_seconds,
                "run_immediately": run_immediately,
                "name": name,
            }
        )

    def run(self) -> None:
        return None

    def stop(self) -> None:
        return None

    @property
    def schedule(self):
        class _Namespace:
            @staticmethod
            def get_jobs():
                return []

        return _Namespace


def _make_started_service(monkeypatch, *, auto_run_enabled: bool):
    config = SimpleNamespace(
        schedule_enabled=True,
        schedule_time="18:00",
        schedule_times=["18:00"],
        agent_event_monitor_enabled=False,
    )
    service = RuntimeSchedulerService(config_provider=lambda: config, owns_schedule=True)
    service._reload_config = lambda: config
    monkeypatch.setattr(runtime_scheduler, "reconcile_stale_scheduled_runs", MagicMock())
    monkeypatch.setattr(runtime_scheduler.threading, "Thread", _NoopThread)

    analysis_calls: List[str] = []
    outcome_calls: List[Any] = []
    monkeypatch.setattr(service, "_run_analysis_once", lambda *a, **k: analysis_calls.append("analysis"))
    monkeypatch.setattr(
        runtime_scheduler,
        "run_skill_opinion_outcome_task",
        lambda *a, **k: outcome_calls.append(k),
    )
    monkeypatch.setattr(runtime_scheduler, "Scheduler", _FakeScheduler)

    def _run_inline(target):
        target()

    monkeypatch.setattr(RuntimeSchedulerService, "_run_in_background_thread", staticmethod(_run_inline))

    if not auto_run_enabled:
        monkeypatch.setenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, "0")
    else:
        monkeypatch.delenv(SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, raising=False)

    service.start()
    return service, analysis_calls, outcome_calls


class TestDailyScheduleAttachment:
    def test_start_registers_composed_daily_task(self, monkeypatch) -> None:
        service, analysis_calls, outcome_calls = _make_started_service(
            monkeypatch, auto_run_enabled=True
        )

        scheduler = service._scheduler
        assert isinstance(scheduler, _FakeScheduler)
        assert scheduler.daily_task == service._run_daily_analysis_and_outcomes
        assert scheduler.daily_task_run_immediately is False

        # Existing background registrations stay untouched.
        names = [entry["name"] for entry in scheduler.background_tasks]
        assert "scheduled_run_reconcile" in names
        assert all(name != "agent_event_monitor" for name in names)

        scheduler.daily_task()

        assert analysis_calls == ["analysis"]
        assert len(outcome_calls) == 1

    def test_daily_hook_skips_outcome_when_env_disables(self, monkeypatch) -> None:
        service, analysis_calls, outcome_calls = _make_started_service(
            monkeypatch, auto_run_enabled=False
        )

        service._scheduler.daily_task()

        assert analysis_calls == ["analysis"]
        assert outcome_calls == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
