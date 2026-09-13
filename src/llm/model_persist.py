"""Persist CLI /model selection — survives restarts and providers.yaml defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.core.coara_home import home_llm_preferences_path
from src.core.config import ConfigManager, resolve_config_home
from src.core.json_store import write_text_atomic
from src.core.types import LLMProfileConfig
from src.llm.profiles import Profile


def _resolve_persist_call_settings(
    config_manager: ConfigManager,
    provider: str,
    model: str,
) -> tuple[int | None, float | None]:
    """Adopt the *target* provider/model best settings (never carry the previous vendor's)."""
    from src.llm.call_defaults import resolve_best_call_defaults

    defaults = resolve_best_call_defaults(provider, model, config_manager=config_manager)
    return defaults.max_tokens, defaults.temperature


def _build_persist_payload(
    config_manager: ConfigManager,
    provider: str,
    model: str,
) -> dict[str, Any]:
    patch_profile: dict[str, Any] = {
        "provider": provider,
        "model": model,
    }
    max_tokens, temperature = _resolve_persist_call_settings(config_manager, provider, model)
    if max_tokens is not None:
        patch_profile["max_tokens"] = max_tokens
    if temperature is not None:
        patch_profile["temperature"] = temperature
    return {
        "default_provider": provider,
        "default_model": model,
        "llm_profiles": {Profile.AGENT_MAIN: patch_profile},
    }


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    write_text_atomic(path, text)


def _preference_write_path(config_manager: ConfigManager) -> Path:
    """Canonical writable path for /model persistence."""
    return home_llm_preferences_path(resolve_config_home(config_manager.get_raw_config()))


def persist_llm_selection(config_manager: ConfigManager, provider: str, model: str) -> Path:
    """Save provider+model to llm_preferences.yaml only (does not touch config.yaml)."""
    from src.llm.service import llm_service

    payload = _build_persist_payload(config_manager, provider, model)
    patch_profile = payload["llm_profiles"][Profile.AGENT_MAIN]

    prefs_path = _preference_write_path(config_manager)
    _write_yaml(prefs_path, payload)

    config_manager.merge_config(payload)
    if config_manager.config is not None:
        config_manager.config.default_provider = provider
        config_manager.config.default_model = model

    config_manager._llm_profiles[Profile.AGENT_MAIN] = LLMProfileConfig(**patch_profile)
    llm_service.configure(config_manager)

    return prefs_path
