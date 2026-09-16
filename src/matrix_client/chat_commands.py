"""Slash commands for Matrix-originated chat.

Delegates to the UI-agnostic :func:`src.coara.commands.execute_command` and
renders the result as plain text via ``send_text``. The fast boolean checks
(``is_new_session_command`` etc.) are retained because
:mod:`src.matrix_client.ingress_helpers` uses them for side-channel filtering
before a turn is even started.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.coara.commands import execute_command

SendText = Callable[[str], Awaitable[Any]]


class _MatrixRoomChannel:
    """绑定房间的 RemoteInteractionChannel 适配器（命令期确认门用）。

    回合内审批经 MatrixRemoteInteractionChannel 靠 turn context 定位房间；命令
    处理不在回合里、无 turn 上下文，必须显式绑定 room_id 才能把审批卡投回
    发起命令的房间。仅实现确认门用到的 send_text/send_approval_* 两个方法。
    """

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
    """True when *body* is a ``/ws`` slash command (incl. remote wrapper).

    Workspace switch must run outside the Matrix turn dispatcher lock so a long
    turn in space A does not block switching to (and chatting in) space B.
    """
    raw = normalize_remote_command_body(body)
    if not raw.startswith("/"):
        return False
    name = raw[1:].split(maxsplit=1)[0].lower()
    return name == "ws"


def _render_output(output: str) -> str:
    """Keep line breaks on mobile markdown without a fenced block (no visible wrapper).

    Multi-line output uses Markdown hard breaks (two trailing spaces) so the app
    renders it as plain text lines; single-line output is sent unchanged.
    """
    stripped = output.strip("\n")
    if "\n" not in stripped:
        return output
    return output.replace("\n", "  \n")


def _matrix_visible_output(result: Any) -> str:
    """Plain text for the phone chat bubble.

    Workspace switch keeps the session note (and optional default-set line) but
    drops the absolute path — that stays in ``result.data`` for CLI.
    """
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
    """Handle owner slash commands. Returns True if *body* was consumed.

    Delegates to the shared command service layer so Matrix gets the same
    command coverage as CLI and Web — no duplicated logic.

    ``room_id``: 命令所在房间。非空时供 B 类会话配置命令的确认门用——把审批
    卡发回该房间（回合外命令无 turn 上下文，room 显式传入才能投递）。
    """
    raw = normalize_remote_command_body(body)
    if not raw.startswith("/"):
        return False

    # matrix 独立视图端（D2）：命令作用于 matrix 自己的视图空间，与全局前台解耦。
    # 测试替身无视图解析方法时退回前台/缺省路径。
    _view_coara = getattr(root, "resolve_matrix_view_coara", None)
    target = _view_coara() if callable(_view_coara) else getattr(root, "foreground_coara", None)
    result = await execute_command(
        root,
        raw,
        target_coara=target,
        origin_source="matrix",
        interaction_channel=_matrix_room_channel(room_id, send_text) if room_id else None,
    )
    if result is None:
        return False

    # Render as plain text (Matrix doesn't support rich markup).
    # Multi-line output is sent as a single message to avoid message spam.
    await send_text(_render_output(_matrix_visible_output(result)))

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
