# -*- coding: utf-8 -*-
"""LiteLLM generation-parameter compatibility helpers."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Kimi K2.6 is consumed through Moonshot's OpenAI-compatible API in this
# repository. Official references:
# - https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart
# - https://platform.moonshot.ai/docs/guide/compatibility#parameters-differences-in-request-body
# - https://huggingface.co/moonshotai/Kimi-K2.6
# - https://docs.litellm.ai/docs/providers/openai_compatible
_FIXED_TEMPERATURE_LITELLM_MODELS: Dict[str, Dict[str, float]] = {
    "kimi-k2.6": {
        "thinking": 1.0,
        "non_thinking": 0.6,
    },
}

# OpenAI reasoning models currently expose these effort labels. ``max`` is
# retained for compatible gateways that advertise it as a provider extension.
LITELLM_REASONING_EFFORT_VALUES = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def normalize_litellm_reasoning_effort(value: Any) -> Optional[str]:
    """Return a supported reasoning effort label or ``None``."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized or normalized not in LITELLM_REASONING_EFFORT_VALUES:
        return None
    return normalized


def parse_litellm_reasoning_efforts(value: Any) -> Dict[str, str]:
    """Parse exact provider-prefixed route aliases to effort labels."""
    if value is None:
        return {}
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return {}
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            logger.warning("LLM_REASONING_EFFORTS_JSON is invalid JSON; ignored")
            return {}
    if not isinstance(value, Mapping):
        logger.warning("LLM_REASONING_EFFORTS_JSON must be a JSON object; ignored")
        return {}

    parsed: Dict[str, str] = {}
    for raw_model, raw_effort in value.items():
        model = str(raw_model).strip()
        if not model or "/" not in model:
            logger.warning(
                "LLM_REASONING_EFFORTS_JSON key %r must be an exact provider-prefixed route alias; ignored",
                raw_model,
            )
            continue
        effort = normalize_litellm_reasoning_effort(raw_effort)
        if effort is None:
            logger.warning(
                "LLM_REASONING_EFFORTS_JSON value for %s is unsupported; allowed values: %s",
                model,
                ", ".join(sorted(LITELLM_REASONING_EFFORT_VALUES)),
            )
            continue
        parsed[model] = effort
    return parsed



@dataclass(frozen=True)
class TemperatureDirective:
    """Request-scoped temperature strategy for one LiteLLM model call."""

    temperature: Optional[float] = None
    omit_temperature: bool = False
    reason: str = ""


@dataclass(frozen=True)
class GenerationParamRecovery:
    """A learned request-parameter repair for a LiteLLM model call."""

    omit_params: Tuple[str, ...] = ()
    set_params: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


_GENERATION_PARAM_RECOVERY_CACHE: Dict[str, GenerationParamRecovery] = {}

_LITELLM_ENDPOINT_PARAM_KEYS = (
    "api_base",
    "base_url",
    "api_version",
    "api_type",
    "azure_endpoint",
    "azure_deployment",
    "deployment_id",
    "custom_llm_provider",
    "organization",
    "region_name",
    "aws_region_name",
    "vertex_project",
    "vertex_location",
    "extra_headers",
    "headers",
    "default_headers",
)
_LITELLM_ROUTING_PARAM_KEYS = ("model", *_LITELLM_ENDPOINT_PARAM_KEYS)
_SECRET_CACHE_FIELD_NAMES = {
    "api_key",
    "authorization",
    "proxy_authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "openai-api-key",
}


def _resolve_litellm_model_list_entry(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the Router model_list entry matching the configured alias."""
    entries = _resolve_litellm_model_list_entries(model, model_list)
    return entries[0] if entries else None


def _resolve_litellm_model_list_entries(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return Router model_list entries matching the configured alias."""
    normalized_model = (model or "").strip()
    if not normalized_model or not model_list:
        return []

    entries: List[Dict[str, Any]] = []
    for entry in model_list:
        model_name = str(entry.get("model_name") or "").strip()
        if not model_name:
            params = entry.get("litellm_params", {}) or {}
            model_name = str(params.get("model") or "").strip()
        if model_name == normalized_model:
            entries.append(entry)
    return entries


def resolve_litellm_wire_model(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Resolve a router alias to its underlying LiteLLM wire model."""
    normalized_model = (model or "").strip()
    if not normalized_model or not model_list:
        return normalized_model

    model_entry = _resolve_litellm_model_list_entry(normalized_model, model_list)
    if not model_entry:
        return normalized_model

    params = model_entry.get("litellm_params", {}) or {}
    wire_model = str(params.get("model") or "").strip()
    if wire_model:
        return wire_model
    return normalized_model


def _extract_reasoning_effort_value(payload: Any) -> Any:
    if isinstance(payload, Mapping) and "reasoning_effort" in payload:
        return payload.get("reasoning_effort")
    return None


def _model_list_reasoning_effort(entry: Mapping[str, Any]) -> Optional[str]:
    """Read DSA metadata first, then tolerate an unsanitized YAML parameter."""
    model_info = entry.get("model_info")
    if isinstance(model_info, Mapping):
        for key in ("dsa_reasoning_effort", "reasoning_effort"):
            if key not in model_info:
                continue
            normalized = normalize_litellm_reasoning_effort(model_info.get(key))
            if normalized is not None:
                return normalized

    params = entry.get("litellm_params")
    if isinstance(params, Mapping):
        normalized = normalize_litellm_reasoning_effort(params.get("reasoning_effort"))
        if normalized is not None:
            return normalized

    normalized = normalize_litellm_reasoning_effort(entry.get("reasoning_effort"))
    return normalized


def resolve_litellm_reasoning_effort(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve one model's reasoning effort without coupling it to a channel."""
    request_value = _extract_reasoning_effort_value(request_overrides)
    normalized_request_value = normalize_litellm_reasoning_effort(request_value)
    if normalized_request_value is not None:
        return normalized_request_value

    for entry in _resolve_litellm_model_list_entries(model, model_list):
        effort = _model_list_reasoning_effort(entry)
        if effort is not None:
            return effort

    normalized_model = (model or "").strip()
    mapped_effort = parse_litellm_reasoning_efforts(os.getenv("LLM_REASONING_EFFORTS_JSON")).get(normalized_model)
    if mapped_effort is not None:
        return mapped_effort
    return normalize_litellm_reasoning_effort(os.getenv("LLM_REASONING_EFFORT"))


def _provider_from_wire_model(model: str) -> str:
    normalized = (model or "").strip().lower()
    if not normalized or "/" not in normalized:
        return "openai"
    return normalized.split("/", 1)[0]


def _resolve_litellm_protocol(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve the DSA channel protocol for request-parameter gating."""
    entries = _resolve_litellm_model_list_entries(model, model_list)
    if entries:
        entry = entries[0]
        model_info = entry.get("model_info")
        if isinstance(model_info, Mapping):
            explicit_protocol = str(model_info.get("dsa_protocol") or "").strip().lower()
            if explicit_protocol:
                return explicit_protocol
        params = entry.get("litellm_params")
        if isinstance(params, Mapping):
            custom_provider = str(params.get("custom_llm_provider") or "").strip().lower()
            if custom_provider:
                return custom_provider
            wire_model = str(params.get("model") or "").strip()
            if wire_model:
                return _provider_from_wire_model(wire_model)

    if isinstance(request_overrides, Mapping):
        custom_provider = str(request_overrides.get("custom_llm_provider") or "").strip().lower()
        if custom_provider:
            return custom_provider
        override_model = str(request_overrides.get("model") or "").strip()
        if override_model:
            return _provider_from_wire_model(override_model)
    return _provider_from_wire_model(model)



def _extract_thinking_config(payload: Optional[Dict[str, Any]]) -> Any:
    """Extract a thinking-mode flag from LiteLLM-style request kwargs."""
    if not isinstance(payload, dict):
        return None
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict) and "thinking" in extra_body:
        return extra_body.get("thinking")
    if "thinking" in payload:
        return payload.get("thinking")
    return None


def _parse_thinking_enabled(value: Any) -> Optional[bool]:
    """Parse thinking-mode config into True/False/unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"enabled", "enable", "true", "1", "on", "thinking"}:
            return True
        if normalized in {"disabled", "disable", "false", "0", "off", "none", "non-thinking", "non_thinking"}:
            return False
        return None
    if isinstance(value, dict):
        if "enabled" in value:
            return _parse_thinking_enabled(value.get("enabled"))
        if "type" in value:
            return _parse_thinking_enabled(value.get("type"))
    return None


def resolve_litellm_thinking_enabled(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Resolve whether the outgoing LiteLLM request explicitly enables thinking."""
    thinking_config = None
    model_entry = _resolve_litellm_model_list_entry(model, model_list)
    if model_entry:
        thinking_config = _extract_thinking_config(model_entry)
        entry_params = model_entry.get("litellm_params", {}) or {}
        entry_thinking_config = _extract_thinking_config(entry_params)
        if entry_thinking_config is not None:
            thinking_config = entry_thinking_config

    override_thinking_config = _extract_thinking_config(request_overrides)
    if override_thinking_config is not None:
        thinking_config = override_thinking_config
    return _parse_thinking_enabled(thinking_config)


def _model_parts(model: str) -> List[str]:
    return [part for part in re.split(r"[/:\s]+", (model or "").lower()) if part]


def _matches_model_family(model: str, family: str) -> bool:
    return any(part == family or part.startswith(f"{family}-") for part in _model_parts(model))


def _should_omit_litellm_temperature(model: str) -> bool:
    """Return whether a model family should rely on the provider default temperature."""
    return any(
        part.startswith(("gpt-5", "gpt5"))
        or part in {"o1", "o3", "o4"}
        or part.startswith(("o1-", "o3-", "o4-"))
        for part in _model_parts(model)
    )


def get_fixed_litellm_temperature(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Return a provider-mandated temperature for known strict models."""
    normalized_model = resolve_litellm_wire_model(model, model_list).lower()
    if not normalized_model:
        return None
    thinking_enabled = resolve_litellm_thinking_enabled(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    for model_name, temperatures in _FIXED_TEMPERATURE_LITELLM_MODELS.items():
        if _matches_model_family(normalized_model, model_name):
            if thinking_enabled is False and temperatures.get("non_thinking") is not None:
                return temperatures["non_thinking"]
            if temperatures.get("thinking") is not None:
                return temperatures["thinking"]
            if temperatures.get("non_thinking") is not None:
                return temperatures["non_thinking"]
    return None


def resolve_litellm_temperature_directive(
    model: str,
    *,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> TemperatureDirective:
    """Resolve the request-scoped temperature directive for a LiteLLM model."""
    fixed_temperature = get_fixed_litellm_temperature(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    if fixed_temperature is not None:
        return TemperatureDirective(
            temperature=fixed_temperature,
            reason="fixed_model_temperature",
        )

    wire_model = resolve_litellm_wire_model(model, model_list)
    if _should_omit_litellm_temperature(wire_model):
        return TemperatureDirective(
            omit_temperature=True,
            reason="provider_default_temperature",
        )
    return TemperatureDirective()


def normalize_litellm_temperature(
    model: str,
    temperature: Optional[float],
    *,
    default: float = 0.7,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> float:
    """Return the legacy float temperature normalization for callers that need it."""
    fixed_temperature = get_fixed_litellm_temperature(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    if fixed_temperature is not None:
        return fixed_temperature
    if temperature is None:
        return default
    return float(temperature)


def _redact_recovery_cache_value(param_name: str, value: Any) -> Any:
    if param_name.strip().lower() in _SECRET_CACHE_FIELD_NAMES:
        return "<set>" if value else "<empty>"
    if isinstance(value, Mapping):
        return {
            str(key): _redact_recovery_cache_value(str(key), nested_value)
            for key, nested_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_recovery_cache_value(param_name, item) for item in value]
    return value


def _stable_recovery_cache_json(value: Mapping[str, Any]) -> str:
    redacted = {
        key: _redact_recovery_cache_value(key, val)
        for key, val in sorted(value.items())
    }
    return json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str)


def _filter_litellm_routing_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: params[key]
        for key in _LITELLM_ROUTING_PARAM_KEYS
        if key in params and params[key] not in (None, "")
    }


def _request_endpoint_cache_scope(request_overrides: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(request_overrides, Mapping):
        return None
    routing_params = _filter_litellm_routing_params(request_overrides)
    if not any(key in routing_params for key in _LITELLM_ENDPOINT_PARAM_KEYS):
        return None
    return _stable_recovery_cache_json(routing_params)


def _model_list_endpoint_cache_scope(
    model: str,
    model_list: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    entries = _resolve_litellm_model_list_entries(model, model_list)
    if not entries:
        return "default"

    fingerprints = set()
    for entry in entries:
        params = entry.get("litellm_params", {}) or {}
        if not isinstance(params, Mapping):
            params = {}
        routing_params = _filter_litellm_routing_params(params)
        if not routing_params:
            routing_params = {"model": str(entry.get("model_name") or model).strip()}
        fingerprints.add(_stable_recovery_cache_json(routing_params))

    if len(fingerprints) != 1:
        return None
    return next(iter(fingerprints))


def _recovery_cache_key(
    model: str,
    *,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    wire_model = resolve_litellm_wire_model(model, model_list).strip().lower()
    thinking_enabled = resolve_litellm_thinking_enabled(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    reasoning_effort = resolve_litellm_reasoning_effort(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    endpoint_scope = _request_endpoint_cache_scope(request_overrides)
    if endpoint_scope is None:
        endpoint_scope = _model_list_endpoint_cache_scope(model, model_list)
    if endpoint_scope is None:
        return None
    return (
        f"{wire_model or (model or '').strip().lower()}"
        f"|thinking={thinking_enabled}"
        f"|reasoning={reasoning_effort}"
        f"|endpoint={endpoint_scope}"
    )


def apply_litellm_param_recovery(
    call_kwargs: Dict[str, Any],
    recovery: GenerationParamRecovery,
) -> Dict[str, Any]:
    """Return kwargs with a learned parameter recovery applied."""
    updated = dict(call_kwargs)
    for param in recovery.omit_params:
        updated.pop(param, None)
    for param, value in recovery.set_params.items():
        updated[param] = value
    return updated


def get_cached_litellm_generation_param_recovery(
    model: str,
    *,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[GenerationParamRecovery]:
    """Return a process-local parameter recovery learned for this model call shape."""
    key = _recovery_cache_key(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    if key is None:
        return None
    return _GENERATION_PARAM_RECOVERY_CACHE.get(key)


def remember_litellm_generation_param_recovery(
    model: str,
    recovery: GenerationParamRecovery,
    *,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> None:
    """Remember a successful parameter recovery for later requests in this process."""
    key = _recovery_cache_key(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )
    if key is None:
        return
    _GENERATION_PARAM_RECOVERY_CACHE[key] = recovery


def clear_litellm_generation_param_recovery_cache() -> None:
    """Clear process-local learned parameter recoveries. Intended for tests."""
    _GENERATION_PARAM_RECOVERY_CACHE.clear()


def apply_litellm_generation_params(
    call_kwargs: Dict[str, Any],
    model: str,
    temperature: Optional[float],
    *,
    default_temperature: float = 0.7,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return kwargs with model-compatible generation parameters applied."""
    updated = dict(call_kwargs)
    effective_overrides = request_overrides if request_overrides is not None else updated
    directive = resolve_litellm_temperature_directive(
        model,
        model_list=model_list,
        request_overrides=effective_overrides,
    )
    if directive.omit_temperature:
        updated.pop("temperature", None)
    elif directive.temperature is not None:
        updated["temperature"] = directive.temperature
    else:
        updated["temperature"] = default_temperature if temperature is None else float(temperature)
    reasoning_effort = resolve_litellm_reasoning_effort(
        model,
        model_list=model_list,
        request_overrides=effective_overrides,
    )
    if reasoning_effort is not None and _resolve_litellm_protocol(
        model,
        model_list=model_list,
        request_overrides=effective_overrides,
    ) == "openai":
        if reasoning_effort == "max":
            # LiteLLM's effort ladder has no native ``max`` tier (upstream maps
            # none/minimal/low/medium/high/xhigh only): the Responses bridge
            # silently drops a ``reasoning_effort=max`` string, and
            # OpenAI-compatible chat gateways reject the unknown tier outright.
            # Emit the provider-native object form on every openai surface so
            # the configured max effort actually reaches the model.
            updated.pop("reasoning_effort", None)
            updated["reasoning"] = {"effort": "max"}
        else:
            updated["reasoning_effort"] = reasoning_effort
    else:
        updated.pop("reasoning_effort", None)
    cached_recovery = get_cached_litellm_generation_param_recovery(
        model,
        model_list=model_list,
        request_overrides=updated,
    )
    if cached_recovery:
        updated = apply_litellm_param_recovery(updated, cached_recovery)
    return updated
