"""WorkflowScheduler — 引擎侧调度门面（内核图唯一执行路径）。

2026-08-16 起 WdlRunner（legacy steps+edges 执行器）退役，本模块只包
KernelGraphRunner：投影文本（nodes+edges）经 core.serde 解析、
core.semantics 激活引擎驱动，SQLite 持久化。save/run 入口的文本必须
已是内核投影格式（export_wdl 的 canonical 输出）。
"""

from __future__ import annotations

from typing import Any

from wdl.core.kernel_runner import KernelGraphRunner
from wdl.persistence import WorkflowOwnershipLost, WorkflowPersistence

__all__ = [
    "WorkflowScheduler",
    "WorkflowOwnershipLost",
]


class WorkflowScheduler:
    """Execute workflows from kernel projection text (nodes + edges)."""

    def __init__(self, persistence: WorkflowPersistence) -> None:
        self.persistence = persistence
        self._runner = KernelGraphRunner(persistence)

    async def cancel(self, instance_id: str) -> None:
        await self._runner.cancel(instance_id)

    async def execute(
        self,
        instance_id: str,
        wdl_text: str,
        inputs: dict[str, Any],
        node_executor,
    ) -> dict[str, Any]:
        return await self._runner.execute(instance_id, wdl_text, inputs, node_executor)

    async def resume(
        self,
        instance_id: str,
        event_data: dict[str, Any] | None = None,
        node_executor=None,
    ) -> dict[str, Any]:
        return await self._runner.resume(instance_id, event_data, node_executor)
