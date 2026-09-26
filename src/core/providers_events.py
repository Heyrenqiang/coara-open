"""providers 配置变更广播（core 层挂载点）。

写入口三处（web 保存 / 配置助手 reload_providers 工具 / 首个 key 对齐默认）
都调 broadcast_providers_changed()；内核启动时经 set_providers_changed_publisher
注入 EventBus.publish——llm/tools 层不能 import coara/ui 拿总线，用注入解耦。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_publisher: Callable[[Any], None] | None = None


def set_providers_changed_publisher(publish: Callable[[Any], None] | None) -> None:
    global _publisher
    _publisher = publish


def broadcast_providers_changed() -> None:
    """发 providers_changed trace 事件（经 trace 通道推 web 配置页即时刷新）。"""
    if _publisher is None:
        return
    from src.core.events import TraceEvent
    from src.core.logger import logger

    try:
        _publisher(
            TraceEvent(
                coara_id="system",
                coara_name="config",
                event_type="providers_changed",
                message="providers configuration changed",
            )
        )
    except Exception:
        logger.debug("providers_changed broadcast failed", exc_info=True)
