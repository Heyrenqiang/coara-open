"""Headless kernel runtime bootstrap (daemon/tray 内核宿主用).

Loads config, initializes providers, and builds the root runtime.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import click
from rich.console import Console

from src.core.config import config_manager
from src.core.logger import setup_logger

console = Console()


def _matrix_console_logging_enabled(*, matrix_flag: bool, config: Any) -> bool:
    """Enable Rich console logging when Matrix remote CLI or notify room is configured."""
    if matrix_flag:
        return True
    matrix_cfg = config.matrix if config else None
    notify_room = (matrix_cfg.notify_room_id if matrix_cfg else "") or os.environ.get("COARA_MATRIX_NOTIFY_ROOM", "")
    return bool(notify_room)


async def bootstrap_runtime(ctx: click.Context):
    """Load config, initialize providers, and build the root runtime."""
    workspace = ctx.obj["workspace"]
    provider = ctx.obj["provider"]
    model = ctx.obj["model"]

    # config 已由调用方（main._run_frontends）加载过则不再重复加载，
    # 避免启动日志里 env/preferences/sandbox 等初始化重复打印。
    if getattr(config_manager, "_config", None) is None:
        await config_manager.load()
    ctx.obj["config"] = config_manager.config
    # 产品化：无 -v/--verbose。console 恒为 WARNING（出错只打一条信息），
    # traceback/DEBUG 只进日志文件。开发者可用 COARA_DEBUG=1 看完整日志（不对外）。
    debug = os.environ.get("COARA_DEBUG") == "1"
    matrix_console = _matrix_console_logging_enabled(
        matrix_flag=bool(ctx.obj.get("matrix_enabled")),
        config=config_manager.config,
    )
    use_console = debug or matrix_console
    setup_logger(
        log_level="DEBUG" if debug else config_manager.config.log_level,
        enable_console=use_console,
        rich_console=console if use_console else None,
        console_level="DEBUG" if debug else "WARNING",
        workspace_dir=workspace,
        coara_home=config_manager.config.coara_home,
        clear_on_start=True,
    )
    ctx.obj["matrix_console_logging"] = matrix_console

    from src.cli.first_run_setup import heal_default_provider_if_needed
    from src.cli.product_defaults import DEFAULT_PROVIDER
    from src.llm.registry import initialize_providers, provider_registry

    # Align default_provider to a key that actually exists before init/fallback tips.
    heal_default_provider_if_needed(console)

    await initialize_providers(config_manager)
    resolved_provider = provider or config_manager.config.default_provider or None
    resolved_model = model or config_manager.config.default_model or None
    if resolved_provider and provider_registry.has(resolved_provider):
        active = provider_registry.get(resolved_provider)
        if not (getattr(active, "api_key", None) or "").strip():
            # Prefer another registered provider that has a real key.
            healed = heal_default_provider_if_needed(console)
            if healed and provider_registry.has(healed):
                resolved_provider = healed
                if not model:
                    with contextlib.suppress(Exception):
                        resolved_model = config_manager.get_provider(healed).default_model or None
            else:
                # 被动引导：启动不主动提示，对话失败时由错误处理引导 /model
                pass
    elif resolved_provider and not provider_registry.has(resolved_provider):
        available = provider_registry.list_available()
        # Prefer providers that actually have an API key loaded.
        keyed = [name for name in available if (getattr(provider_registry.get(name), "api_key", None) or "").strip()]
        pick_list = keyed or available
        if pick_list:
            fallback = pick_list[0]
            for preferred in (DEFAULT_PROVIDER, "openai", "anthropic"):
                if preferred in pick_list:
                    fallback = preferred
                    break
            console.print(
                f"[yellow]提示：默认供应商 '{resolved_provider}' 不可用，改用 '{fallback}'。"
                f"已写入 llm_preferences（或请在 system\\.env 补齐密钥）。[/yellow]"
            )
            with contextlib.suppress(Exception):
                from src.cli.first_run_setup import write_default_provider

                write_default_provider(fallback)
            resolved_provider = fallback
            if not model:
                try:
                    resolved_model = config_manager.get_provider(fallback).default_model or None
                except Exception:
                    resolved_model = None
    from src.coara.root import create_root_coara

    root = await create_root_coara(
        workspace_dir=workspace,
        provider_name=resolved_provider,
        model=resolved_model,
        workspace_alias=ctx.obj.get("workspace_alias"),
    )
    with contextlib.suppress(Exception):
        from src.records import loading_phrases
        from src.records.daily_curator import agent_dir_from_root

        agent_dir = agent_dir_from_root(root)
        if agent_dir is not None:
            loading_phrases.set_custom_phrases_path(agent_dir / loading_phrases.CUSTOM_PHRASES_FILENAME)
            loading_phrases.reshuffle()
    return root, resolved_provider or "", resolved_model or ""
