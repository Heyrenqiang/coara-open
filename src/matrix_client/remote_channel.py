"""Matrix implementation of :class:`~src.coara.remote_channel.RemoteInteractionChannel`."""

from __future__ import annotations

from typing import Any

from src.core.abort import AbortSignal


class MatrixRemoteInteractionChannel:
    """Reuses ``turn`` callbacks; delegates approval semantics to ApprovalCenter."""

    _confirm_source = "matrix"

    async def send_text(self, body: str) -> bool:
        from src.coara.turn_context import get_turn_channel_id, get_turn_send_text

        channel_id = get_turn_channel_id()
        send_text = get_turn_send_text()
        if not channel_id or send_text is None:
            return False
        result = await send_text(channel_id, body)
        # Legacy callbacks may return None on success.
        return result is not False

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        """投递审批请求帧：发 m.coara.approval 卡片到回合房间（无回合房间即失败）。"""
        from src.coara.turn_context import get_end_channel, get_turn_channel_id
        from src.matrix_client.approval_bridge import deliver_approval_request_frame

        room = str(get_turn_channel_id() or "").strip() or None
        if not room:
            end = get_end_channel()
            room = str(getattr(end, "channel_id", "") or "").strip() or None
        return await deliver_approval_request_frame(frame, room_id=room)

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        """终态帧：发 m.coara.approval_resolved 到原卡房间（手机端关闭/置灰卡片）。"""
        from src.matrix_client.approval_bridge import deliver_approval_resolved_frame

        return await deliver_approval_resolved_frame(frame)

    async def confirm(
        self,
        question: str,
        options: list[dict[str, Any]],
        *,
        timeout_seconds: float = 600.0,
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


MATRIX_REMOTE_INTERACTION_CHANNEL = MatrixRemoteInteractionChannel()
