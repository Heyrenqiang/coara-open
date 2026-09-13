"""Frontend capability adapter for the kernel (phase 0).

内核模块不直接 import ``src.cli.*``。前端（CLI）启动时经 ``configure_frontend()``
注入真实实现；headless / 非交互内核保持安全 no-op 默认。

各类能力按调用方分组：渲染类（scrollback/diff/notify）默认 no-op；交互类
（prompt_select / run_add_model_flow）默认抛 RuntimeError，复用现有降级链
（Matrix 常驻审批通道 / 非 tty 自动放行）；其余回调默认保守取值。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


async def _default_prompt_select(**kwargs: Any) -> Any:
    # 无 CLI modal 时抛 RuntimeError，触发现有降级链（远程通道 / 常驻 Matrix / 非 tty 放行）。
    raise RuntimeError("CLI prompt not registered")


async def _default_run_add_model_flow(console: Any | None = None) -> str | None:
    # headless 下无交互式的「添加模型」流程，返回 None 视作取消。
    return None


@dataclass
class FrontendHooks:
    """内核对前端显示/交互能力的接入点。所有字段均有安全默认实现。"""

    display_width: Callable[[str], int] = lambda text: len(text)
    get_diff_colors: Callable[[], Any] = lambda: None
    scrollback_write_renderable: Callable[[Any], None] = lambda renderable: None
    scrollback_write: Callable[[str], None] = lambda text: None
    notify_block_rendered_after_tool_line: Callable[[], None] = lambda: None
    flush_interaction_echo: Callable[..., str | None] = lambda *args, **kwargs: None
    prompt_select: Callable[..., Awaitable[Any]] = _default_prompt_select
    run_add_model_flow: Callable[..., Awaitable[str | None]] = _default_run_add_model_flow
    system_env_path: Callable[[], Any] = lambda: None
    write_api_key: Callable[[Any, str, str], None] = lambda env, name, val: None
    console: Any = None
    theme: Any = None


_FRONTEND = FrontendHooks()


def get_frontend() -> FrontendHooks:
    """当前登记的前端能力（默认安全实现，CLI 启动后注入真实实现）。"""
    return _FRONTEND


def configure_frontend(**hooks: Any) -> None:
    """注入/覆盖前端能力。CLI 启动时调用；未知字段直接报错避免静默拼错。"""
    for key, value in hooks.items():
        if not hasattr(_FRONTEND, key):
            raise AttributeError(f"unknown frontend hook: {key}")
        setattr(_FRONTEND, key, value)
