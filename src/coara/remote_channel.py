"""Transport-agnostic remote interaction channel (UI + outbound text during a turn).

审批语义集中在 :class:`~src.coara.approval_center.ApprovalCenter`；通道是哑管道——
只负责把 ApprovalCenter 下发的帧送达本端、把本端回执交回 Center，不持有任何
pending 状态与业务逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.core.abort import AbortSignal


class RemotePromptDeliveryError(Exception):
    """Outbound interaction prompt could not be delivered to the remote client."""


@runtime_checkable
class RemoteInteractionChannel(Protocol):
    """Adapter for tool approval on a remote ingress turn."""

    async def send_text(self, body: str) -> bool:
        """Push plain text to the remote user. Return False if delivery failed."""

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        """投递审批请求帧（ApprovalCenter.request_frame 的产物）到本端。

        Return False if delivery failed（Center 据此 fail-closed）。
        """

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        """投递审批终态帧（approval_id + outcome）让本端关掉在渲染的卡片。"""

    async def confirm(
        self,
        question: str,
        options: list[dict[str, Any]],
        *,
        timeout_seconds: float = 300.0,
        signal: AbortSignal | None = None,
    ) -> bool:
        """Legacy yes/no confirm（vault 提示等沿用）；审批走 ApprovalCenter。"""
        ...
