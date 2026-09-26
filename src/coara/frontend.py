"""Frontend capability adapter for the kernel. 内核模块不直接 import ``src.cli.*``"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


async def _default_prompt_select(**kwargs: Any) -> Any:
    # 无 CLI modal 时抛 RuntimeError，触发现有降级链（远程通道 / 常驻 Matrix / 非 tty 放行）。
    raise RuntimeError("CLI prompt not registered")


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
