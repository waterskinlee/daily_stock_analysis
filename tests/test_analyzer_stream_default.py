# -*- coding: utf-8 -*-
"""Default streaming behavior for the legacy analyzer generation wrapper."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.analyzer import GeminiAnalyzer  # noqa: E402
from src.llm.generation_params import litellm_analysis_stream_enabled  # noqa: E402


def _make_inst(captured: dict) -> GeminiAnalyzer:
    inst = GeminiAnalyzer.__new__(GeminiAnalyzer)
    backend = SimpleNamespace(
        generate=lambda *a, **k: (
            captured.update(k),
            SimpleNamespace(text="t", model="m", usage={}),
        )[1]
    )
    inst.get_generation_backend_config_error = lambda: None
    inst._can_use_generation_fallback = lambda err: False
    inst._resolve_generation_backend_config = lambda: ("litellm", None)
    inst._get_generation_backend = lambda bid: backend
    return inst


def test_helper_defaults_on_and_env_kill_switch(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ANALYSIS_STREAM", raising=False)
    assert litellm_analysis_stream_enabled() is True
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("LLM_ANALYSIS_STREAM", value)
        assert litellm_analysis_stream_enabled() is False


def test_call_litellm_defaults_to_streaming(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ANALYSIS_STREAM", raising=False)
    captured: dict = {}
    _make_inst(captured)._call_litellm("p", {})
    assert captured["stream"] is True


def test_call_litellm_env_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ANALYSIS_STREAM", "0")
    captured: dict = {}
    _make_inst(captured)._call_litellm("p", {})
    assert captured["stream"] is False


def test_explicit_stream_false_overrides_default(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ANALYSIS_STREAM", raising=False)
    captured: dict = {}
    _make_inst(captured)._call_litellm("p", {}, stream=False)
    assert captured["stream"] is False



def test_consume_litellm_stream_captures_final_usage_chunk() -> None:
    from types import SimpleNamespace as NS

    inst = GeminiAnalyzer.__new__(GeminiAnalyzer)
    inst._normalize_usage = lambda usage, **_kw: (
        {"total_tokens": usage["total_tokens"]} if usage else {}
    )

    def chunk(delta=None, usage=None):
        return NS(choices=[NS(delta=NS(content=delta))], usage=usage)
    text, usage = inst._consume_litellm_stream(
        [
            chunk("hel"),
            chunk("lo"),
            chunk(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}),
        ],
        model="openai/x-preview-f-free",
    )

    assert text == "hello"
    assert usage == {"total_tokens": 5}