"""Slash commands for Matrix-originated chat"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.coara.commands import execute_command

SendText = Callable[[str], Awaitable[Any]]


class _MatrixRoomChannel:
    """绑定房间的 RemoteInteractionChannel 适配器（命令期确认门用）"""

    _confirm_source = "matrix"

    def __init__(self, room_id: str, send_text: SendText) -> None:
        self._room_id = room_id
        self._send_text = send_text

    async def send_text(self, body: str) -> bool:
        await self._send_text(body)
        return True

    async def send_approval_request(self, frame: dict[str, Any]) -> bool:
        from src.matrix_client.approval_bridge import deliver_approval_request_frame

        return await deliver_approval_request_frame(frame, room_id=self._room_id)

    async def send_approval_resolved(self, frame: dict[str, Any]) -> bool:
        from src.matrix_client.approval_bridge import deliver_approval_resolved_frame

        return await deliver_approval_resolved_frame(frame)


def _matrix_room_channel(room_id: str, send_text: SendText) -> _MatrixRoomChannel | None:
    """构造绑定房间的确认通道；room_id 空时返回 None（确认门退化为无通道）。"""
    if not room_id:
        return None
    return _MatrixRoomChannel(room_id=room_id, send_text=send_text)


def normalize_remote_command_body(body: str) -> str:
    """Trim whitespace so ``/new`` works from remote ingress（来源标签已废弃，无需剥壳）。"""
    return body.strip()


def is_new_session_command(body: str) -> bool:
    """True when *body* is a /new session boundary command (incl. remote wrapper)."""
    return normalize_remote_command_body(body).lower() == "/new"


def is_stop_command(body: str) -> bool:
    """True when *body* is a /stop turn-interrupt command (incl. remote wrapper)."""
    return normalize_remote_command_body(body).lower() == "/stop"


def is_ws_command(body: str) -> bool:
    """True when *body* is a ``/ws`` slash command (incl"""
    raw = normalize_remote_command_body(body)
    if not raw.startswith("/"):
        return False
    name = raw[1:].split(maxsplit=1)[0].lower()
    return name == "ws"


def _render_output(output: str) -> str:
    """Keep line breaks on mobile markdown without a fenced block (no visible wrapper)"""
    stripped = output.strip("\n")
    if "\n" not in stripped:
        return output
    return output.replace("\n", "  \n")


def _matrix_visible_output(result: Any) -> str:
    """Plain text for the phone chat bubble"""
    output = str(getattr(result, "output", "") or "")
    if getattr(result, "action", None) != "switch_workspace":
        return output
    data = getattr(result, "data", None) or {}
    path = str(data.get("workspace_dir") or "").strip()
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if path:
        lines = [ln for ln in lines if ln.strip() != path]
    return "\n".join(lines) if lines else output


async def try_handle_matrix_chat_command(
    root: Any,
    body: str,
    *,
    send_text: SendText,
    room_id: str = "",
) -> bool:
    """Handle owner slash commands."""
    raw = normalize_remote_command_body(body)
    if not raw.startswith("/"):
        return False

    # matrix 独立视图端（D2）：命令作用于 matrix 自己的视图空间，与全局前台解耦。
    # 测试替身无视图解析方法时退回前台/缺省路径。
    _view_coara = getattr(root, "resolve_matrix_view_coara", None)
    target = _view_coara() if callable(_view_coara) else getattr(root, "foreground_coara", None)
    if room_id:
        # /compact 等慢命令要跑 LLM：包 turn scope，结束发 [COARA_TURN] 信封，
        # 手机端靠它点亮/熄灭 typing（入站本身点灯，信封灭灯）。
        from src.matrix_client.turn_signal import matrix_turn_scope

        async with matrix_turn_scope(room_id, send_chunk=lambda _rid, body: send_text(body)):
            result = await execute_command(
                root,
                raw,
                target_coara=target,
                origin_source="matrix",
                interaction_channel=_matrix_room_channel(room_id, send_text),
            )
    else:
        result = await execute_command(
            root,
            raw,
            target_coara=target,
            origin_source="matrix",
            interaction_channel=None,
        )
    if result is None:
        return False

    # 静默命令（零正文，意图在 data 里）不向房间发空气泡。
    visible = _matrix_visible_output(result)
    if visible.strip():
        await send_text(_render_output(visible))

    # Hidden structured payloads for the mobile slash-command panels
    # (interactive model / workspace pickers and thinking status in the Android app).
    from src.coara.mobile_sync import (
        build_models_payload,
        build_status_payload,
        build_thinking_payload,
        build_usage_payload,
        build_workspaces_payload,
    )

    command_name = raw[1:].split(maxsplit=1)[0].lower()
    try:
        if command_name == "model":
            await send_text(build_models_payload(root))
            # Model switch may change whether thinking levels apply (e.g. Kimi K3).
            await send_text(build_thinking_payload(root))
            await send_text(build_status_payload(root))
        elif command_name == "ws":
            await send_text(build_workspaces_payload(root))
            # Workspace switch may change provider/model (per-slot config), so
            # refresh the phone's model/thinking panels alongside the list.
            await send_text(build_models_payload(root))
            await send_text(build_thinking_payload(root))
            await send_text(build_status_payload(root))
        elif command_name == "thinking":
            await send_text(build_thinking_payload(root))
        elif command_name == "usage":
            await send_text(build_usage_payload(root))
        elif command_name == "status":
            await send_text(build_status_payload(root))
        elif command_name == "new":
            await send_text(build_usage_payload(root))
            await send_text(build_status_payload(root))
    except Exception as exc:  # sync payloads are best-effort; never break the command
        from src.core.logger import logger

        logger.debug(f"mobile sync payload skipped: {exc}")
    return True
