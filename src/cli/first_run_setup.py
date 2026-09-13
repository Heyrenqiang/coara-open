"""首次启动 API key 配置向导（CLI）。

触发条件：
- 没有任何可用 API key → 交互向导
- 默认 provider 无可用 key，但其他 provider 有 → 自动对齐默认并持久化（不弹窗）

占位符密钥（your-key / xxx / changeme 等）视为未配置。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

from src.cli.product_defaults import DEFAULT_PROVIDER
from src.core.coara_home import home_llm_preferences_path
from src.core.config import _iter_env_file_paths, config_manager
from src.core.json_store import write_text_atomic

_SKIP = "__skip__"

# Common docs / template placeholders that must not count as real keys.
_PLACEHOLDER_KEYS = frozenset(
    {
        "",
        "your-key",
        "your_key",
        "your-api-key",
        "your_api_key",
        "xxx",
        "xxxx",
        "sk-xxx",
        "sk-xxxx",
        "changeme",
        "change-me",
        "placeholder",
        "todo",
        "none",
        "null",
    }
)


def is_usable_api_key(value: str | None) -> bool:
    """True when the env value looks like a real API key (not empty / placeholder)."""
    raw = (value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in _PLACEHOLDER_KEYS:
        return False
    if lowered.startswith(("your-", "your_", "xxx", "sk-xxx", "sk-your")):
        return False
    return not ("changeme" in lowered or "your_key" in lowered or "your-key" in lowered)


def _configured_providers() -> list[Any]:
    """Enabled providers in YAML declaration order (matching ``/model``)."""
    providers = getattr(config_manager, "_providers", None) or {}
    return [p for p in providers.values() if getattr(p, "enabled", True)]


def provider_has_usable_key(provider: Any) -> bool:
    # Inline api_key (Web UI) counts; otherwise fall back to the env var.
    inline = (getattr(provider, "api_key", "") or "").strip()
    if inline:
        return is_usable_api_key(inline)
    return is_usable_api_key(os.getenv(getattr(provider, "api_key_env", "") or "", ""))


def providers_missing_keys() -> list[Any]:
    """Return enabled providers whose API key is empty or placeholder."""
    return [p for p in _configured_providers() if not provider_has_usable_key(p)]


def providers_with_usable_keys() -> list[Any]:
    return [p for p in _configured_providers() if provider_has_usable_key(p)]


def _preferred_usable(usable: list[Any]) -> Any:
    """Prefer declaration order, then product DEFAULT_PROVIDER, else first."""
    if not usable:
        raise IndexError("no usable providers")
    for p in usable:
        if p.name == DEFAULT_PROVIDER:
            return p
    return usable[0]


def system_env_path() -> Path:
    """Target .env path for the wizard (first search path = <coara_home>/system/.env)."""
    return _iter_env_file_paths(None)[0]


def write_api_key(env_path: Path, env_name: str, value: str) -> None:
    """Write or replace an env assignment in the given .env file.

    Existing assignments (including commented-out placeholders) are replaced
    in place; otherwise the assignment is appended. Written atomically via a
    same-directory temp file + replace so a crash mid-write can't truncate
    the whole .env (which would drop every other key).
    """
    from src.core.json_store import write_text_atomic

    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        candidate = line.strip().lstrip("#").strip()
        if candidate.startswith(f"{env_name}=") or candidate.startswith(f"{env_name} ="):
            lines[index] = f"{env_name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{env_name}={value}")
    write_text_atomic(env_path, "\n".join(lines) + "\n")


def write_default_provider(provider_name: str) -> Path:
    """Persist default_provider so the next launch / this session use the chosen one.

    Keeps the persisted trio consistent: a provider-only write would leave a split
    pair (e.g. ``agnes`` + ``MiniMax-M2.7-highspeed``) whenever the previous model
    belongs to another vendor — the status bar then shows a combo that cannot run,
    and agent.main still drives off the old provider.
    """
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


def heal_default_provider_if_needed(console: Any | None = None) -> str | None:
    """If default has no usable key but another provider does, switch and persist.

    Returns the new provider name when healed, else None.
    """
    usable = providers_with_usable_keys()
    if not usable:
        return None
    default = getattr(getattr(config_manager, "_config", None), "default_provider", "") or ""
    by_name = {p.name: p for p in _configured_providers()}
    if default and default in by_name and provider_has_usable_key(by_name[default]):
        return None
    pick = _preferred_usable(usable)
    if pick.name == default:
        return None
    prefs = write_default_provider(pick.name)
    if console is not None:
        console.print(f"[dim]默认 provider 已对齐为 {pick.name}（已有可用 API key；{prefs}）[/dim]")
    return pick.name


async def run_add_model_flow(console: Any | None = None) -> str | None:
    """统一的「添加模型」流程：选已支持厂商 → 填 API key → 写 .env 生效并设默认。

    首发启动与 /model --add 共用同一套逻辑（绝不两套）。provider 已在
    providers.yaml 声明（deepseek/kimi/zhipu/minimax…，各带 api_key_env、
    base_url、默认模型），用户只需选厂商、填 key——url 与模型都不用管。
    返回成功配置的 provider 名；取消/失败返回 None。不负责切换模型（调用方
    按需处理：首发由 root 创建后读默认 provider，/model --add 里再 switch_llm）。
    """
    providers = _configured_providers()
    if not providers:
        return None
    try:
        import questionary
    except ImportError:
        return None

    default_provider = getattr(getattr(config_manager, "_config", None), "default_provider", "") or ""
    choices = []
    for p in providers:
        has = "（已配置）" if provider_has_usable_key(p) else ""
        choices.append(questionary.Choice(title=f"{p.name}{has} · {p.api_key_env}", value=p.name))
    choices.append(questionary.Choice(title="稍后再说（手动编辑 system\\.env）", value=_SKIP))

    selected = await questionary.select(
        "选择要添加密钥的厂商：",
        choices=choices,
        default=default_provider if default_provider in {p.name for p in providers} else providers[0].name,
    ).ask_async()
    if not selected or selected == _SKIP:
        if console is not None:
            console.print(f"[dim]已跳过。之后可输 /model 选「＋ 添加模型」，或编辑 {system_env_path()}[/dim]")
        return None

    provider = next(p for p in providers if p.name == selected)
    if provider_has_usable_key(provider):
        write_default_provider(provider.name)
        if console is not None:
            console.print(f"[green]已使用现有密钥，默认厂商设为 {provider.name}[/green]")
        return provider.name

    key = (await questionary.password(f"填入 {provider.api_key_env}：").ask_async() or "").strip()
    if not key or not is_usable_api_key(key):
        if console is not None:
            console.print("[yellow]未填写有效 API key，已取消[/yellow]")
        return None

    env_path = system_env_path()
    write_api_key(env_path, provider.api_key_env, key)
    os.environ[provider.api_key_env] = key  # 当前进程立即生效
    write_default_provider(provider.name)
    if console is not None:
        console.print(f"[green]已写入 {env_path}，默认厂商设为 {provider.name}[/green]")
    return provider.name


async def maybe_run_first_run_setup(console: Any) -> bool:
    """首次启动：无可用 key 时进入统一的「添加模型」流程。配置成功返回 True。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
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
    console.print("[bold cyan]首次使用 · 添加模型[/bold cyan]")
    console.print("还没有可用的 LLM API key。选一个厂商，填入密钥即可开始——url 与模型都不用管。")
    # Web 配置页直达链接（含 token，点击即开）；解析失败静默降级，不影响向导
    try:
        from src.ui.web_link import build_web_url

        web_url = build_web_url(Path.cwd(), "/config")
    except Exception:
        web_url = None
    if web_url:
        console.print(f"[dim]也可以在浏览器里配置：[/dim][cyan][link={web_url}]{web_url}[/link][/cyan]")
    console.print()
    configured = await run_add_model_flow(console)
    if configured:
        console.print()
    return configured is not None
