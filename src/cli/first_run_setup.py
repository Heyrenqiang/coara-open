"""首次启动模型配置引导（CLI）"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from src.core.api_keys import is_usable_api_key as _is_usable_api_key
from src.core.coara_home import home_llm_preferences_path
from src.core.config import config_manager
from src.core.json_store import write_text_atomic


def _configured_providers() -> list[Any]:
    """Enabled providers in YAML declaration order (matching ``/model``)."""
    providers = getattr(config_manager, "_providers", None) or {}
    return [p for p in providers.values() if getattr(p, "enabled", True)]


def provider_has_usable_key(provider: Any) -> bool:
    # Inline api_key (Web UI) counts; otherwise fall back to the env var.
    inline = (getattr(provider, "api_key", "") or "").strip()
    if inline:
        return _is_usable_api_key(inline)
    return _is_usable_api_key(os.getenv(getattr(provider, "api_key_env", "") or "", ""))


def providers_missing_keys() -> list[Any]:
    """Return enabled providers whose API key is empty or placeholder."""
    return [p for p in _configured_providers() if not provider_has_usable_key(p)]


def providers_with_usable_keys() -> list[Any]:
    return [p for p in _configured_providers() if provider_has_usable_key(p)]


def _preferred_usable(usable: list[Any]) -> Any:
    """Prefer declaration order: first enabled provider with a usable key."""
    if not usable:
        raise IndexError("no usable providers")
    return usable[0]


def write_default_provider(provider_name: str) -> Path:
    """Persist default_provider so the next launch / this session use the chosen one"""
    home = getattr(getattr(config_manager, "_config", None), "coara_home", None)
    if home is None:
        from src.core.coara_home import resolve_coara_home

        home = resolve_coara_home(Path.cwd())
    prefs_path = home_llm_preferences_path(Path(home))
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if prefs_path.exists():
        try:
            data = yaml.safe_load(prefs_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data["default_provider"] = provider_name

    merge: dict[str, Any] = {"default_provider": provider_name}
    providers = {p.name: p for p in _configured_providers()}
    provider = providers.get(provider_name)
    if provider is not None:
        models_cfg = getattr(provider, "models", None) or {}
        available = {
            str(entry.get("id") or "") for entry in (models_cfg.get("available") or []) if isinstance(entry, dict)
        }
        model = str(data.get("default_model") or "")
        if model not in available:
            model = str(models_cfg.get("default") or getattr(provider, "default_model", "") or "").strip()
            # 即使为空也写：清掉属于别家 provider 的旧模型，空值由配置层回退处理
            data["default_model"] = model
        # 运行时走 agent.main：它指向无可用 key 的 provider 时，只改默认标签等于没治
        profiles = data.get("llm_profiles")
        main = profiles.get("agent.main") if isinstance(profiles, dict) else None
        if isinstance(main, dict) and model:
            main_provider = providers.get(str(main.get("provider") or ""))
            if main_provider is None or not provider_has_usable_key(main_provider):
                main["provider"] = provider_name
                main["model"] = model
                from src.llm.model_persist import _resolve_persist_call_settings

                max_tokens, temperature = _resolve_persist_call_settings(config_manager, provider_name, model)
                if max_tokens is not None:
                    main["max_tokens"] = max_tokens
                if temperature is not None:
                    main["temperature"] = temperature
                merge["llm_profiles"] = {"agent.main": dict(main)}
    if "default_model" in data:
        merge["default_model"] = data["default_model"]

    write_text_atomic(
        prefs_path,
        yaml.safe_dump(data, allow_unicode=True, default_flow_style=False),
    )
    config_manager.merge_config(merge)
    if getattr(config_manager, "_config", None) is not None:
        config_manager._config.default_provider = provider_name  # noqa: SLF001
        if "default_model" in merge:
            config_manager._config.default_model = merge["default_model"]  # noqa: SLF001
    if "llm_profiles" in merge:
        from src.core.types import LLMProfileConfig
        from src.llm.profiles import Profile

        config_manager._llm_profiles[Profile.AGENT_MAIN] = LLMProfileConfig(  # noqa: SLF001
            **merge["llm_profiles"]["agent.main"]
        )
    return prefs_path


def clear_default_provider() -> Path | None:
    """清空持久化的默认模型绑定（所有 provider 均无可用 key 时调用）"""
    home = getattr(getattr(config_manager, "_config", None), "coara_home", None)
    if home is None:
        from src.core.coara_home import resolve_coara_home

        home = resolve_coara_home(Path.cwd())
    prefs_path = home_llm_preferences_path(Path(home))
    if not prefs_path.exists():
        return None
    try:
        data = yaml.safe_load(prefs_path.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.pop("default_provider", None)
    data.pop("default_model", None)
    # agent.main 指向无 key provider 时一并摘掉 provider/model，保留 thinking 等其它字段
    profiles = data.get("llm_profiles")
    main = profiles.get("agent.main") if isinstance(profiles, dict) else None
    if isinstance(main, dict):
        main_provider = main.get("provider")
        providers = {p.name: p for p in _configured_providers()}
        p_obj = providers.get(str(main_provider or ""))
        if p_obj is None or not provider_has_usable_key(p_obj):
            main.pop("provider", None)
            main.pop("model", None)
    write_text_atomic(
        prefs_path,
        yaml.safe_dump(data, allow_unicode=True, default_flow_style=False),
    )
    config_manager.merge_config({"default_provider": "", "default_model": ""})
    if getattr(config_manager, "_config", None) is not None:
        config_manager._config.default_provider = ""  # noqa: SLF001
        config_manager._config.default_model = ""  # noqa: SLF001
    return prefs_path


def heal_default_provider_if_needed(console: Any | None = None) -> str | None:
    """If default has no usable key but another provider does, switch and persist.

    Returns the new provider name when healed, else None.
    """
    default = getattr(getattr(config_manager, "_config", None), "default_provider", "") or ""
    by_name = {p.name: p for p in _configured_providers()}
    usable = providers_with_usable_keys()
    if not usable:
        # 发布版重启仍「接上」早已失效的 kimi）。
        if default and (default not in by_name or not provider_has_usable_key(by_name[default])):
            clear_default_provider()
        return None
    if default and default in by_name and provider_has_usable_key(by_name[default]):
        return None
    pick = _preferred_usable(usable)
    if pick.name == default:
        return None
    prefs = write_default_provider(pick.name)
    if console is not None:
        console.print(f"[dim]默认 provider 已对齐为 {pick.name}（已有可用 API key；{prefs}）[/dim]")
    return pick.name


def _is_tty(stream: Any) -> bool:
    """流是否连着终端；None / 无 isatty 一律按否处理。

    内核自重启拉起的子进程可能没有标准流（sys.stdin 为 None），裸 .isatty()
    会抛 AttributeError 把新实例带走——首次配置只是便利步骤，不该有这种权力。
    """
    probe = getattr(stream, "isatty", None)
    if not callable(probe):
        return False
    with contextlib.suppress(Exception):
        return bool(probe())
    return False


async def maybe_run_first_run_setup(console: Any) -> bool:
    """首次启动：无可用 key 时提示去 Web 配置页「模型」，不在终端填 key"""
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        heal_default_provider_if_needed(None)
        return False

    providers = _configured_providers()
    if not providers:
        return False

    usable = providers_with_usable_keys()
    if usable:
        heal_default_provider_if_needed(console)
        return False

    console.print()
    console.print("[bold cyan]首次使用 · 配置模型[/bold cyan]")
    console.print("还没有可用的 LLM API key。请到配置页「模型」添加厂商密钥。")
    try:
        from src.ui.web_link import build_web_url

        web_url = build_web_url(Path.cwd(), "/config?focus=models")
    except Exception:
        web_url = None
    if web_url:
        console.print(f"[dim]浏览器打开：[/dim][cyan][link={web_url}]{web_url}[/link][/cyan]")
    console.print()
    return False
