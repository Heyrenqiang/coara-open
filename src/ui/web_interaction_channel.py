"""Web implementation of :class:`~src.coara.remote_channel.RemoteInteractionChannel`.

哑管道：审批语义在 :class:`~src.coara.approval_center.ApprovalCenter`，本类只负责
把 Center 的帧送到活跃浏览器连接、把回执帧交给 Center。断连挂起不取消——重连
后经 ``redeliver_pending`` 用同一 approval_id 重发，Center 的 future 照常等待。

WS 帧协议::

    服务端 → 浏览器 {"type": "approval_request", "approval_id", "question", "options",
                    "timeout_s", "workspace", "created_at_ms"}
    浏览器 → 服务端 {"type": "approval_reply", "approval_id", "approved"}
    服务端 → 浏览器 {"type": "approval_resolved", "approval_id", "outcome"}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.coara.turn_context import get_turn_channel_id, get_turn_send_text
from src.core.logger import logger
from src.ui.web_socket_registry import WebSocketRegistry

if TYPE_CHECKING:
    from src.core.abort import AbortSignal


class WebRemoteInteractionChannel:
    """Implements ``RemoteInteractionChannel`` Protocol via WebSocket."""

    _confirm_source = "web"

    def __init__(self, registry: WebSocketRegistry) -> None:
        self._registry = registry
        # approval_id → 重发用原帧（断连重投；future 在 ApprovalCenter）
        self._pending_frames: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # RemoteInteractionChannel Protocol implementation
    # ------------------------------------------------------------------

    async def send_text(self, body: str) -> bool:
        """Push plain text to the browser (e.g. plan summary before buttons)."""
        send_text = get_turn_send_text()
        channel_id = get_turn_channel_id()
        if channel_id and send_text is not None:
            result = await send_text(channel_id, body)
            return result is not False
        return await self._registry.send_to_active({"type": "info", "text": body})

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        """投递审批请求帧到活跃浏览器连接。"""
        if self._registry.active_connection is None:
            logger.warning("Web approval requested but no active browser connection")
            return False
        message = {"type": "approval_request", **frame}
        sent = await self._registry.send_to_active(message)
        if not sent:
            logger.warning("Web approval: failed to send prompt to browser")
            return False
        self._pending_frames[str(frame["approval_id"])] = message
        return True

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        """终态帧：浏览器据此关掉在渲染的审批 Modal。"""
        approval_id = str(frame.get("approval_id") or "")
        self._pending_frames.pop(approval_id, None)
        return await self._registry.send_to_active({"type": "approval_resolved", **frame})

    def mark_connection_disconnected(self, conn_id: str) -> None:
        """断连只标记不取消：Center 的 future 继续等（超时/abort 兜底）。"""
        logger.debug(f"Web connection {conn_id} disconnected; pending approvals held for redelivery")

    async def redeliver_pending(self) -> int:
        """重连后把全部在途审批帧重发给新活跃连接（同一 approval_id）。"""
        if not self._pending_frames:
            return 0
        active = self._registry.active_connection
        if active is None:
            return 0
        from src.coara.approval_center import get_approval_center

        center = get_approval_center()
        delivered = 0
        for approval_id, message in list(self._pending_frames.items()):
            if await self._registry.send_to_active(dict(message)):
                center.rebind_reply_actor(str(approval_id), active.conn_id)
                delivered += 1
            else:
                self._pending_frames.pop(approval_id, None)
        if delivered:
            logger.debug(f"Redelivered {delivered} pending approval(s) after browser reconnect")
        return delivered

    # ------------------------------------------------------------------
    # confirm（vault 提示等；审批走 ApprovalCenter）
    # ------------------------------------------------------------------

    async def confirm(
        self,
        question: str,
        options: list[dict[str, Any]],
        *,
        timeout_seconds: float = 300.0,
        signal: AbortSignal | None = None,
    ) -> bool:
        from src.coara.approval_center import get_approval_center

        return await get_approval_center().request(
            question=question,
            options=options,
            timeout_seconds=timeout_seconds,
            signal=signal,
            channel=self,
        )
