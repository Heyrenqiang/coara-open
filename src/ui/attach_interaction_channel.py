"""Attach（外挂 CLI）端的 RemoteInteractionChannel 实现"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.coara.remote_channel import RemotePromptDeliveryError
from src.core.logger import logger

if TYPE_CHECKING:
    from src.core.abort import AbortSignal


class AttachRemoteInteractionChannel:
    """``/ws/attach`` 连接上的审批通道本体（跨连接持有重发帧表）"""

    def __init__(self, server: Any) -> None:
        # server: WebServer（仅消费 attach_registry 定向发送能力）
        self._server = server
        # approval_id → (conn_id, 原帧)：重连重发用；future 在 ApprovalCenter
        self._pending_frames: dict[str, tuple[str, dict[str, Any]]] = {}

    def for_connection(self, conn_id: str) -> _BoundAttachInteractionChannel:
        """绑定到指定 attach 连接的协议适配器（turn() 的 interaction_channel 入参）。"""
        return _BoundAttachInteractionChannel(self, conn_id)

    async def send_approval_request(self, frame: dict[str, Any], *, conn_id: str) -> bool:
        """内部实现：向指定连接推送审批请求帧。协议侧走 for_connection 绑定版。"""
        if not conn_id:
            raise RemotePromptDeliveryError("attach 审批缺少目标连接（conn_id 为空）。")
        message = {"type": "approval_request", **frame}
        sent = await self._server.attach_registry.send_to(conn_id, message)
        if not sent:
            return False
        self._pending_frames[str(frame["approval_id"])] = (conn_id, message)
        return True

    async def send_approval_resolved(self, frame: dict[str, Any], *, conn_id: str) -> bool:
        """尽力推终态帧让对端关掉弹窗（连接已断则静默跳过）。"""
        import contextlib

        approval_id = str(frame.get("approval_id") or "")
        self._pending_frames.pop(approval_id, None)
        with contextlib.suppress(Exception):
            return bool(await self._server.attach_registry.send_to(conn_id, {"type": "approval_resolved", **frame}))
        return False

    def pending_for_connection(self, conn_id: str) -> list[dict[str, Any]]:
        """重连回放的待重发审批帧（断连挂起不取消）。"""
        return [dict(message) for cid, message in self._pending_frames.values() if cid == conn_id]

    async def redeliver_pending(self, conn_id: str) -> int:
        """重连后把本连接在途审批帧重发（同一 approval_id）"""
        messages = self.pending_for_connection(conn_id)
        if not messages:
            return 0
        from src.coara.approval_center import get_approval_center

        center = get_approval_center()
        delivered = 0
        for message in messages:
            approval_id = str(message.get("approval_id") or "")
            record = center.get(approval_id) if approval_id else None
            if record is not None and record.state != "pending":
                self._pending_frames.pop(approval_id, None)
                continue
            if await self._server.attach_registry.send_to(conn_id, message):
                center.rebind_reply_actor(approval_id, conn_id)
                delivered += 1
            else:
                self._pending_frames.pop(approval_id, None)
        if delivered:
            logger.debug(f"Redelivered {delivered} pending approval(s) to attach conn {conn_id}")
        return delivered

    def mark_connection_disconnected(self, conn_id: str) -> None:
        """断连只标记不取消：Center 的 future 继续等（超时/abort 兜底），帧留待重连回放。"""
        logger.debug(f"Attach connection {conn_id} disconnected; pending approvals held for redelivery")

    def drop_frames_for_connection(self, conn_id: str) -> None:
        """连接显式注销时清掉其重发帧（Center 侧状态由超时/abort 收口）。"""
        for approval_id, (cid, _) in list(self._pending_frames.items()):
            if cid == conn_id:
                self._pending_frames.pop(approval_id, None)


class _BoundAttachInteractionChannel:
    """绑定单条 attach 连接的 ``RemoteInteractionChannel`` 协议适配器。"""

    _confirm_source = "cli-attached"

    def __init__(self, owner: AttachRemoteInteractionChannel, conn_id: str) -> None:
        self._owner = owner
        self._conn_id = conn_id

    async def send_text(self, body: str) -> bool:
        """推纯文本到本连接（对齐协议；attach 正文主流走 EndRegistry，此为兜底）。"""
        return await self._owner._server.attach_registry.send_to(self._conn_id, {"type": "info", "text": body})

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        return await self._owner.send_approval_request(frame, conn_id=self._conn_id)

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        return await self._owner.send_approval_resolved(frame, conn_id=self._conn_id)

    async def confirm(
        self,
        question: str,
        options: list[dict[str, Any]],
        *,
        timeout_seconds: float = 300.0,
        signal: AbortSignal | None = None,
        tool_call_id: str = "",
    ) -> bool:
        from src.coara.approval_center import get_approval_center

        return await get_approval_center().request(
            question=question,
            options=options,
            timeout_seconds=timeout_seconds,
            signal=signal,
            channel=self,
            tool_call_id=tool_call_id,
        )
