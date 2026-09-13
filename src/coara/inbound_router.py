"""UnifiedScheduler message routing for RootCoara.

消息内容体系下，事件/提醒/后台完成均不再入队叫醒主会话：
事件源与提醒到点 → 收件箱（见 event_sources/manager 与 root_lifecycle）；
后台任务完成 → 定向进驻发起会话历史（见 RootCoara._subscribe_background_task_notifications）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


async def dispatch_scheduler_message(root: RootCoara, msg: Any) -> None:
    """Route a single UnifiedScheduler message."""
    del root
    # 历史 ``process_result``（工作流终态回投）随 WDL 执行层剥离（2026-09-08）
    # 不再有生产者：工作流终态由独立 WDL 软件自管。其余未知类型同样忽略。
    logger.debug(f"Scheduler message ignored (msg_type={msg.msg_type})")


async def run_scheduler_consumer_loop(root: RootCoara) -> None:
    """Background loop to process the FIFO queue."""
    async for msg in root.scheduler.listen():
        await dispatch_scheduler_message(root, msg)
