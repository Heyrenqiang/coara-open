"""UnifiedScheduler message routing for RootCoara."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


async def dispatch_scheduler_message(root: RootCoara, msg: Any) -> None:
    """Route a single UnifiedScheduler message."""
    del root
    # 不再有生产者：工作流终态由独立 WDL 软件自管。其余未知类型同样忽略。
    logger.debug(f"Scheduler message ignored (msg_type={msg.msg_type})")


async def run_scheduler_consumer_loop(root: RootCoara) -> None:
    """Background loop to process the FIFO queue."""
    async for msg in root.scheduler.listen():
        await dispatch_scheduler_message(root, msg)
