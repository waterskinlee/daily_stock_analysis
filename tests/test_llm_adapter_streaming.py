# -*- coding: utf-8 -*-
"""Streaming assembly tests for the agent analysis path (llm_adapter)."""
from __future__ import annotations

import sys
import unittest.mock as mock

sys.path.insert(0, __import__("os").path.abspath(__import__("os").path.join(__import__("os").path.dirname(__file__), "..")))

from src.agent.llm_adapter import (
    LLMToolAdapter,
    _accumulate_stream_response,
    _analysis_stream_enabled,
)


def _chunk(delta=None, finish_reason=None, **extra):
    from types import SimpleNamespace

    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    payload = {"choices": [choice]}
    payload.update(extra)
    return SimpleNamespace(**payload)


def test_analysis_stream_enabled_by_default_and_kill_switch(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ANALYSIS_STREAM", raising=False)
    assert _analysis_stream_enabled() is True
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("LLM_ANALYSIS_STREAM", value)
        assert _analysis_stream_enabled() is False


def test_accumulate_stream_assembles_content_reasoning_and_usage() -> None:
    usage = {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}
    stream = [
        _chunk(delta={"content": "Hel"}),
        _chunk(delta={"reasoning_content": "thinking ", "content": "lo"}),
        _chunk(delta={}),
        _chunk(finish_reason="stop", usage=usage),
    ]

    synthetic = _accumulate_stream_response(stream)

    assert synthetic.choices[0].message.content == "Hello"
    assert synthetic.choices[0].message.reasoning_content == "thinking "
    assert synthetic.choices[0].finish_reason == "stop"
    assert synthetic.usage == usage


def test_accumulate_stream_assembles_tool_call_fragments_by_index() -> None:
    def fragment(index, **kw):
        base = {"index": index, "function": {}}
        base.update(kw)
        fn = base.pop("function")
        base["function"] = {
            "name": fn.get("name"),
            "arguments": fn.get("arguments"),
        }
        return base

    stream = [
        _chunk(delta={"tool_calls": [
            fragment(0, id="call_a", function={"name": "get_stock", "arguments": '{"sto'}),
            fragment(1, id="call_b", function={"name": "se", "arguments": ""}),
        ]}),
        _chunk(delta={"tool_calls": [
            fragment(1, function={"name": "arch", "arguments": '{"q":2}'}),
            fragment(0, function={"arguments": 'ck":"x"}'}),
        ]}),
        _chunk(finish_reason="tool_calls"),
    ]

    synthetic = _accumulate_stream_response(stream)

    calls = synthetic.choices[0].message.tool_calls
    assert [tc.id for tc in calls] == ["call_a", "call_b"]
    assert calls[0].function.name == "get_stock"
    assert calls[0].function.arguments == '{"stock":"x"}'
    assert calls[1].function.name == "search"
    assert calls[1].function.arguments == '{"q":2}'


def test_accumulate_stream_accepts_object_style_chunks() -> None:
    from types import SimpleNamespace

    delta = SimpleNamespace(content="obj", reasoning_content=None, tool_calls=None)
    stream = [SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")])]

    synthetic = _accumulate_stream_response(stream)

    assert synthetic.choices[0].message.content == "obj"


def test_accumulate_stream_raises_on_truncated_empty_output() -> None:
    import pytest

    stream = [_chunk(delta={}, finish_reason="length")]
    with pytest.raises(RuntimeError, match="finish_reason='length'"):
        _accumulate_stream_response(stream)


def test_accumulate_stream_raises_on_truncated_partial_output() -> None:
    import pytest

    stream = [
        _chunk(delta={"content": "partial answer that got cut"}),
        _chunk(finish_reason="length"),
    ]
    with pytest.raises(RuntimeError, match="truncated by max_tokens"):
        _accumulate_stream_response(stream)


def test_parse_litellm_response_accepts_synthetic_stream_result() -> None:
    adapter = LLMToolAdapter.__new__(LLMToolAdapter)
    adapter._config = SimpleNamespace(llm_model_list=[])

    synthetic = _accumulate_stream_response([
        _chunk(delta={
            "content": "",
            "tool_calls": [{
                "index": 0,
                "id": "call_a",
                "function": {"name": "echo", "arguments": '{"a": 1}'},
            }],
        }),
        _chunk(finish_reason="tool_calls", usage={"prompt_tokens": 3}),
    ])

    response = adapter._parse_litellm_response(synthetic, "openai/x-preview-f-free")

    assert response.content == ""
    assert response.tool_calls[0].name == "echo"
    assert response.tool_calls[0].arguments == {"a": 1}
    assert response.usage


def _make_adapter():
    adapter = LLMToolAdapter.__new__(LLMToolAdapter)
    adapter._config = SimpleNamespace(
        llm_temperature=0.2,
        llm_model_list=[],
        llm_prompt_cache_diagnostics_level="off",
    )
    adapter._router = None
    adapter._has_channel_config = lambda: False
    adapter._route_resolution = SimpleNamespace(model_list=[], primary_model=None)
    return adapter


def test_call_litellm_model_streams_by_default() -> None:
    adapter = _make_adapter()
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)

        def gen():
            yield _chunk(delta={"content": "stream-"})
            yield _chunk(delta={"content": "ok"}, finish_reason="stop")

        return iter(gen())

    with mock.patch("src.agent.llm_adapter.litellm.completion", side_effect=completion), \
            mock.patch("src.agent.llm_adapter.get_api_keys_for_model", return_value=[]), \
            mock.patch("src.agent.llm_adapter.extra_litellm_params", return_value={}), \
            mock.patch("src.agent.llm_adapter.register_fallback_model_pricing"), \
            mock.patch("src.agent.llm_adapter.get_effective_agent_primary_model", return_value=None):
        response = adapter._call_litellm_model(
            [{"role": "user", "content": "hi"}],
            [],
            "openai/x-preview-f-free",
            models_tried=["openai/x-preview-f-free"],
        )

    assert captured.get("stream") is True
    assert captured.get("stream_options") == {"include_usage": True}
    assert response.content == "stream-ok"


def test_call_litellm_model_kill_switch_keeps_non_streaming(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ANALYSIS_STREAM", "0")
    adapter = _make_adapter()
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(
            content="plain",
            tool_calls=None,
            reasoning_content=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    with mock.patch("src.agent.llm_adapter.litellm.completion", side_effect=completion), \
            mock.patch("src.agent.llm_adapter.get_api_keys_for_model", return_value=[]), \
            mock.patch("src.agent.llm_adapter.extra_litellm_params", return_value={}), \
            mock.patch("src.agent.llm_adapter.register_fallback_model_pricing"), \
            mock.patch("src.agent.llm_adapter.get_effective_agent_primary_model", return_value=None):
        response = adapter._call_litellm_model(
            [{"role": "user", "content": "hi"}],
            [],
            "openai/x-preview-f-free",
            models_tried=["openai/x-preview-f-free"],
        )

    assert "stream" not in captured
    assert response.content == "plain"


from types import SimpleNamespace  # noqa: E402  (shared fixtures above)
