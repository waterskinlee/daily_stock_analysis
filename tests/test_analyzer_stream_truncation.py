# -*- coding: utf-8 -*-
"""Truncation guard tests for the legacy analyzer stream consumer."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.analyzer import GeminiAnalyzer, _LiteLLMStreamError


def _make_inst() -> GeminiAnalyzer:
    inst = GeminiAnalyzer.__new__(GeminiAnalyzer)
    inst._normalize_usage = lambda usage, **_kw: (
        {"total_tokens": usage["total_tokens"]} if usage else {}
    )
    return inst


def _chunk(delta, finish_reason=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


def test_consume_raises_partial_received_on_length_final_chunk() -> None:
    """Length-final chunk arrives with an EMPTY delta body."""
    inst = _make_inst()
    stream = [
        _chunk(SimpleNamespace(content="half a sent")),
        _chunk(SimpleNamespace(content=""), finish_reason="length"),
    ]
    with pytest.raises(_LiteLLMStreamError) as exc_info:
        inst._consume_litellm_stream(stream, model="openai/x-preview-f-free")
    assert exc_info.value.partial_received is True
    assert exc_info.value.truncated is True
    assert "truncated by max_tokens" in str(exc_info.value)


def test_consume_captures_finish_reason_before_empty_delta_skip() -> None:
    """Ordering guarantee: a chunk with NO delta attribute at all must still
    register its finish_reason — capture happens before the empty-delta skip.
    """
    inst = _make_inst()
    stream = [
        _chunk(SimpleNamespace(content="text")),
        SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="length")]
        ),  # no delta attribute whatsoever
    ]
    with pytest.raises(_LiteLLMStreamError) as exc_info:
        inst._consume_litellm_stream(stream, model="openai/x-preview-f-free")
    assert exc_info.value.partial_received is True
    assert exc_info.value.truncated is True


def test_consume_accepts_stop_finished_stream() -> None:
    """No false positive on normal completion."""
    inst = _make_inst()
    stream = [
        _chunk(SimpleNamespace(content="complete answer")),
        _chunk(None, finish_reason="stop"),
    ]
    text, _usage = inst._consume_litellm_stream(stream, model="openai/x-preview-f-free")
    assert text == "complete answer"



def test_impl_skips_same_model_retry_on_truncated_stream() -> None:
    """Truncation is deterministic: next model must be tried with NO
    intermediate same-model non-stream attempt.
    """
    inst = GeminiAnalyzer.__new__(GeminiAnalyzer)
    calls: list[str] = []

    def fake_dispatch(model, call_kwargs, **_kwargs):
        calls.append(model)
        if model == "openai/primary":

            def gen():
                yield _chunk(SimpleNamespace(content="half"))
                yield _chunk(SimpleNamespace(content=""), finish_reason="length")

            return iter(gen())
        message = SimpleNamespace(content="fallback ok", tool_calls=None, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    inst._dispatch_litellm_completion = fake_dispatch
    inst._config_override = SimpleNamespace(
        litellm_model="openai/primary",
        litellm_fallback_models=["openai/fallback"],
        llm_model_list=[],
    )
    inst._router = None
    inst._legacy_router_model_list = []

    text, model_used, _usage = inst._call_litellm("prompt", {"max_tokens": 128})

    assert text == "fallback ok"
    assert model_used == "openai/fallback"
    assert calls == ["openai/primary", "openai/fallback"]