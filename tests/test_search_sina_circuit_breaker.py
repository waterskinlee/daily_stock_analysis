# -*- coding: utf-8 -*-
"""SinaNews provider rate-limit circuit breaker tests."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import MagicMock, patch

from src.search_service import SinaNewsSearchProvider


def _rate_limited_response() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(status_code=429)


def _ok_response(items: list) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(status_code=200, json=lambda: {"data": {"list": items}})


def test_sina_circuit_opens_after_429_and_skips_until_cooldown() -> None:
    provider = SinaNewsSearchProvider(enabled=True)
    assert provider.is_available is True

    with patch(
        "src.search_service.requests.get",
        return_value=MagicMock(status_code=429),
    ):
        first = provider.search("贵州茅台 600519 股票 最新消息")

    assert first.success is False
    assert "429" in (first.error_message or "")
    # Circuit opens immediately after the rate limit.
    assert provider.is_available is False

    with patch("src.search_service.requests.get") as guarded:
        second = provider.search("贵州茅台 600519 股票 最新消息")

    assert second.success is False
    assert "限流" in (second.error_message or "")
    guarded.assert_not_called()


def test_sina_circuit_resets_on_success() -> None:
    provider = SinaNewsSearchProvider(enabled=True)
    with patch(
        "src.search_service.requests.get",
        return_value=MagicMock(status_code=429),
    ):
        provider.search("q1")
    assert provider.is_available is False

    provider._cooldown_until = 0.0
    items = [{
        "title": "t",
        "intro": "i",
        "url": "https://finance.sina.com.cn/x",
        "ctime": 0,
        "media_show": "新浪财经",
    }]
    with patch("src.search_service.requests.get", return_value=_ok_response(items)):
        ok = provider.search("q2")

    assert ok.success is True
    assert provider.is_available is True


def test_sina_disabled_provider_stays_unavailable() -> None:
    provider = SinaNewsSearchProvider(enabled=False)
    assert provider.is_available is False
