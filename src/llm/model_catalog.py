"""Discover and resolve LLM models from configured providers."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.config import ConfigManager
from src.core.errors import ConfigError, ProviderNotFoundError
from src.core.types import LLMProviderConfig


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A selectable provider + model pair."""

    provider: str
    model_id: str
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @property
    def display(self) -> str:
        """用户可见格式：provider·model（中点分隔）。"""
        return f"{self.provider}·{self.model_id}"


# Provider display order = declaration order in providers.yaml (user-arranged
# in the Web UI settings). ``/model`` lists only enabled providers in that order.


def _provider_has_api_key(cfg: LLMProviderConfig) -> bool:
    """有 API key 就算能用（内联 key 或对应环境变量已设置）。"""
    import os

    if cfg.api_key:
        return True
    return bool(cfg.api_key_env and os.getenv(cfg.api_key_env, ""))


def _enabled_provider_names(config_manager: ConfigManager) -> list[str]:
    """Enabled providers with a usable API key, in YAML declaration order."""
    names: list[str] = []
    for name in config_manager.list_providers():
        try:
            cfg = config_manager.get_provider(name)
        except ProviderNotFoundError:
            continue
        if getattr(cfg, "enabled", True) and _provider_has_api_key(cfg):
            names.append(name)
    return names


def _model_ids_for_provider(cfg: LLMProviderConfig) -> list[str]:
    ids: list[str] = []
    available = cfg.models.get("available")
    if isinstance(available, list):
        for entry in available:
            if isinstance(entry, dict) and entry.get("id"):
                ids.append(str(entry["id"]))
            elif isinstance(entry, str) and entry.strip():
                ids.append(entry.strip())
    default = cfg.default_model or cfg.models.get("default")
    if default and default not in ids:
        ids.insert(0, str(default))
    return ids


def list_model_choices(config_manager: ConfigManager) -> list[ModelChoice]:
    """All models declared under enabled ``providers``, in declaration order."""
    choices: list[ModelChoice] = []
    seen: set[tuple[str, str]] = set()
    for provider_name in _enabled_provider_names(config_manager):
        cfg = config_manager.get_provider(provider_name)
        for model_id in _model_ids_for_provider(cfg):
            key = (provider_name, model_id)
            if key in seen:
                continue
            seen.add(key)
            choices.append(
                ModelChoice(
                    provider=provider_name,
                    model_id=model_id,
                    label=f"{provider_name}·{model_id}",
                )
            )
    return choices


def resolve_model_selection(
    config_manager: ConfigManager,
    selection: str,
    model_id: str | None = None,
) -> tuple[str, str]:
    """Resolve CLI ``/model`` arguments to ``(provider, model)``."""
    selection = selection.strip()
    if not selection:
        raise ConfigError("Empty model selection")

    if model_id:
        provider_name = selection
        if provider_name not in config_manager.list_providers():
            raise ProviderNotFoundError(provider_name)
        return provider_name, model_id.strip()

    providers = config_manager.list_providers()
    if selection in providers:
        cfg = config_manager.get_provider(selection)
        resolved = cfg.default_model or cfg.models.get("default") or ""
        if not resolved:
            raise ConfigError(f"Provider '{selection}' has no default model")
        return selection, str(resolved)

    choices = list_model_choices(config_manager)
    by_key = [c for c in choices if c.key == selection or c.label == selection]
    if len(by_key) == 1:
        return by_key[0].provider, by_key[0].model_id

    if "/" in selection:
        provider_name, _, model_name = selection.partition("/")
        provider_name = provider_name.strip()
        model_name = model_name.strip()
        if provider_name and model_name:
            if provider_name not in providers:
                raise ProviderNotFoundError(provider_name)
            return provider_name, model_name

    by_model = [c for c in choices if c.model_id == selection]
    if len(by_model) == 1:
        return by_model[0].provider, by_model[0].model_id
    if len(by_model) > 1:
        keys = ", ".join(c.key for c in by_model)
        raise ConfigError(f"Model '{selection}' is ambiguous ({keys}); use /model <provider> <model>")

    raise ConfigError(f"Unknown model or provider '{selection}'")


def resolve_model_by_index(config_manager: ConfigManager, index: int) -> tuple[str, str]:
    """1-based index into :func:`list_model_choices`."""
    choices = list_model_choices(config_manager)
    if index < 1 or index > len(choices):
        if not choices:
            raise ConfigError("没有可用模型。请先输入 /model --add 添加模型")
        raise ConfigError(f"模型序号 {index} 超出范围（1–{len(choices)}），输入 /model 查看列表")
    choice = choices[index - 1]
    return choice.provider, choice.model_id
