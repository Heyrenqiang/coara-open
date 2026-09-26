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
    # 保留文件里已有的其它字段（如 thinking 档位），整文件覆盖会误删
    existing = _read_yaml(prefs_path)
    existing.setdefault("llm_profiles", {}).setdefault(Profile.AGENT_MAIN, {})
    existing["default_provider"] = payload["default_provider"]
    existing["default_model"] = payload["default_model"]
    existing["llm_profiles"][Profile.AGENT_MAIN].update(patch_profile)
    _write_yaml(prefs_path, existing)

    config_manager.merge_config(payload)
    if config_manager.config is not None:
        config_manager.config.default_provider = provider
        config_manager.config.default_model = model

    config_manager._llm_profiles[Profile.AGENT_MAIN] = LLMProfileConfig(
        **existing["llm_profiles"][Profile.AGENT_MAIN]
    )
    llm_service.configure(config_manager)

    return prefs_path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def persist_thinking_setting(config_manager: ConfigManager, value: str) -> Path:
    """持久化思考档位（on/off/low/medium/high）到 llm_preferences.yaml 的 agent.main。

    空串表示清除（回到厂商默认）。装载进运行时基线由 llm_service.configure 完成。
    """
    from src.llm.service import llm_service

    prefs_path = _preference_write_path(config_manager)
    existing = _read_yaml(prefs_path)
    profile = existing.setdefault("llm_profiles", {}).setdefault(Profile.AGENT_MAIN, {})
    value = str(value or "").strip().lower()
    if value:
        profile["thinking"] = value
    else:
        profile.pop("thinking", None)
    _write_yaml(prefs_path, existing)

    # 同步内存态：merge 会保留旧 key，清除时必须显式 pop raw + typed profile
    raw_profiles = config_manager._raw_config.setdefault("llm_profiles", {})
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}
        config_manager._raw_config["llm_profiles"] = raw_profiles
    raw_main = raw_profiles.setdefault(Profile.AGENT_MAIN, {})
    if not isinstance(raw_main, dict):
        raw_main = {}
        raw_profiles[Profile.AGENT_MAIN] = raw_main
    if value:
        raw_main["thinking"] = value
        config_manager.merge_config({"llm_profiles": {Profile.AGENT_MAIN: dict(profile)}})
    else:
        raw_main.pop("thinking", None)

    if Profile.AGENT_MAIN in config_manager._llm_profiles:
        current = config_manager._llm_profiles[Profile.AGENT_MAIN].model_dump()
        if value:
            current["thinking"] = value
        else:
            current["thinking"] = ""
        updated = LLMProfileConfig(**current)
        config_manager._llm_profiles[Profile.AGENT_MAIN] = updated
        # CoaraConfig.llm_profiles 是 configure 的真源，必须同步，否则清除后仍装载旧值
        if config_manager._config is not None:
            config_manager._config.llm_profiles[Profile.AGENT_MAIN] = updated
    llm_service.configure(config_manager)

    return prefs_path
