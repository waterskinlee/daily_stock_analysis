"""Shared cooperative-cancellation primitives for analysis execution."""

from __future__ import annotations

from typing import Callable


class AnalysisCancelledError(RuntimeError):
    """Raised when an analysis should stop at its next safe checkpoint."""


def raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    """Raise the shared cancellation exception when the caller requested stop."""
    if cancel_check is not None and cancel_check():
        raise AnalysisCancelledError("分析任务已取消")
