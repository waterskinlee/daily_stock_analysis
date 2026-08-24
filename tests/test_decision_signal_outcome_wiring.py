# -*- coding: utf-8 -*-
"""Wiring tests for the decision-signal outcome scheduled auto-run."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.services import runtime_scheduler
from src.services.runtime_scheduler import (
    DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV,
    SKILL_OPINION_OUTCOME_AUTO_RUN_ENV,
    RuntimeSchedulerService,
    _is_decision_signal_outcome_auto_run_enabled,
    run_decision_signal_outcome_task,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _outcome_run_result() -> Dict[str, Any]:
    return {
        "items": [
            {
                "id": 1,
                "signal_id": 7,
                "horizon": "3d",
                "engine_version": "decision-signal-v1",
                "eval_status": "completed",
                "outcome": "hit",
            }
        ],
        "evaluated": 2,
        "created": 1,
        "updated": 1,
        "skipped": 0,
    }


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
        "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService",
        _StubScheduledService,
    )
    return init_calls, run_calls


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
        name: str | None = None,
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


# ---------------------------------------------------------------------------
# Auto-run gate
# ---------------------------------------------------------------------------


class TestDecisionSignalAutoRunGate:
    @pytest.mark.parametrize("value", [None, "", "1", "true", "TRUE", "yes"])
    def test_enabled_by_default_or_truthy(self, monkeypatch, value) -> None:
        if value is None:
            monkeypatch.delenv(DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, raising=False)
        else:
            monkeypatch.setenv(DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, value)
        assert _is_decision_signal_outcome_auto_run_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
    def test_disabled_by_explicit_falsy(self, monkeypatch, value) -> None:
        monkeypatch.setenv(DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, value)
        assert _is_decision_signal_outcome_auto_run_enabled() is False


class TestDecisionSignalOutcomeTask:
    def test_returns_result_and_defaults_to_limit_100(self, monkeypatch) -> None:
        monkeypatch.delenv(DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, raising=False)
        expected = _outcome_run_result()
        init_calls, run_calls = _install_service_module_stub(monkeypatch, result=expected)

        result = run_decision_signal_outcome_task()

        assert result == expected
        assert init_calls == [True]
        assert run_calls == [{"limit": 100}]

    def test_noops_when_env_disables_auto_run(self, monkeypatch) -> None:
        monkeypatch.setenv(DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, "0")
        init_calls, run_calls = _install_service_module_stub(
            monkeypatch, result=_outcome_run_result()
        )

        result = run_decision_signal_outcome_task()

        assert result is None
        assert init_calls == []
        assert run_calls == []

    def test_swallows_service_exception(self, monkeypatch) -> None:
        monkeypatch.delenv(DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, raising=False)
        _install_service_module_stub(monkeypatch, error=RuntimeError("db unavailable"))

        result = run_decision_signal_outcome_task()  # must not raise

        assert result is None


# ---------------------------------------------------------------------------
# Daily scheduler attachment
# ---------------------------------------------------------------------------


def _make_started_service(monkeypatch, *, skill_auto_run: bool, signal_auto_run: bool):
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
    skill_calls: List[Any] = []
    signal_calls: List[Any] = []
    monkeypatch.setattr(service, "_run_analysis_once", lambda *a, **k: analysis_calls.append("analysis"))
    monkeypatch.setattr(
        runtime_scheduler,
        "run_skill_opinion_outcome_task",
        lambda *a, **k: skill_calls.append(k),
    )
    monkeypatch.setattr(
        runtime_scheduler,
        "run_decision_signal_outcome_task",
        lambda *a, **k: signal_calls.append(k),
    )
    monkeypatch.setattr(runtime_scheduler, "Scheduler", _FakeScheduler)

    def _run_inline(target):
        target()

    monkeypatch.setattr(RuntimeSchedulerService, "_run_in_background_thread", staticmethod(_run_inline))

    for env_name, enabled in (
        (SKILL_OPINION_OUTCOME_AUTO_RUN_ENV, skill_auto_run),
        (DECISION_SIGNAL_OUTCOME_AUTO_RUN_ENV, signal_auto_run),
    ):
        if enabled:
            monkeypatch.delenv(env_name, raising=False)
        else:
            monkeypatch.setenv(env_name, "0")

    service.start()
    return service, analysis_calls, skill_calls, signal_calls


class TestDailyScheduleAttachment:
    def test_start_fires_both_outcome_passes(self, monkeypatch) -> None:
        service, analysis_calls, skill_calls, signal_calls = _make_started_service(
            monkeypatch, skill_auto_run=True, signal_auto_run=True
        )

        scheduler = service._scheduler
        assert isinstance(scheduler, _FakeScheduler)
        assert scheduler.daily_task == service._run_daily_analysis_and_outcomes

        scheduler.daily_task()

        assert analysis_calls == ["analysis"]
        assert len(skill_calls) == 1
        assert len(signal_calls) == 1

    def test_daily_hook_skips_decision_signal_when_env_disables(self, monkeypatch) -> None:
        service, analysis_calls, skill_calls, signal_calls = _make_started_service(
            monkeypatch, skill_auto_run=True, signal_auto_run=False
        )

        service._scheduler.daily_task()

        assert analysis_calls == ["analysis"]
        assert len(skill_calls) == 1
        assert signal_calls == []

    def test_daily_hook_skips_skill_when_only_signal_enabled(self, monkeypatch) -> None:
        service, analysis_calls, skill_calls, signal_calls = _make_started_service(
            monkeypatch, skill_auto_run=False, signal_auto_run=True
        )

        service._scheduler.daily_task()

        assert analysis_calls == ["analysis"]
        assert skill_calls == []
        assert len(signal_calls) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
