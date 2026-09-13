"""Outbound file delivery: Matrix + Web, routed without coupling transports.

``OutboundFileRouter`` is the bridge ``send_file`` talks to. Matrix and Web
bridges plug in independently; attaching one never clears the other.

Routing rules (``target=auto``) deliberately keep the phone path unchanged:

- Explicit ``room_id`` → Matrix only
- Active Web remote turn → Web only
- Active Matrix (or other non-Web) remote turn → Matrix only
- CLI / no remote context → no delivery (local end has no file rendering)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from src.coara.turn_context import get_turn_channel, get_turn_channel_id


@dataclass(slots=True)
class OutboundOwner:
    """发起本次投递的那次调用的归属（谁要的这份文件）。

    - ``workspace_dir`` / ``session_id``：产出归属哪条空间线、哪个会话；
    - ``turn_id``：落在该会话的哪个回合里（工具行/卡片同回合）。
    工具调用经 ``bind_runtime_context`` 拿到这三项（见 ``core.tool_base``）；投递
    可能异步完成（视频要等几分钟），**绝不能**改用「完成那一刻的端视图」——
    那正是「nx 请求的视频落到 v8 线」的根因。
    """

    workspace_dir: str = ""
    session_id: str = ""
    turn_id: str = ""

    @property
    def complete(self) -> bool:
        """归属是否足以定位一条空间线（缺一即不可落带）。"""
        return bool(self.workspace_dir and self.session_id)


@runtime_checkable
class OutboundFileBridge(Protocol):
    """Minimal contract for a single delivery transport.

    ``owner`` is the attribution of the call that asked for this file (its
    workspace/session/turn). Transports that can bind a card to a space line
    MUST use it instead of guessing from the current view.
    """

    async def send_file(
        self,
        path: Path,
        *,
        caption: str = "",
        room_id: str = "",
        owner: OutboundOwner | None = None,
    ) -> dict[str, Any]: ...


def _is_web_remote_channel(channel: object | None) -> bool:
    if channel is None:
        return False
    # Avoid importing web UI at module load (Matrix-only processes).
    cls_name = type(channel).__name__
    if cls_name == "WebRemoteInteractionChannel":
        return True
    module = type(channel).__module__ or ""
    return module.endswith("web_interaction_channel")


def resolve_outbound_targets(
    target: str,
    *,
    has_matrix: bool,
    has_web: bool,
    room_id: str = "",
) -> list[str]:
    """Return ordered destination keys: ``matrix`` and/or ``web``.

    Raises ``ValueError`` when the requested destination is unavailable.
    """
    normalized = (target or "auto").strip().lower() or "auto"
    if normalized not in {"auto", "matrix", "web"}:
        raise ValueError(f"send_file target 无效：{target!r}（可用 auto / matrix / web）")

    if normalized == "matrix":
        if not has_matrix:
            raise ValueError("当前未连接 Matrix，无法发送到手机端")
        return ["matrix"]
    if normalized == "web":
        if not has_web:
            raise ValueError("当前 Web 端未就绪，无法发送到浏览器")
        return ["web"]

    # auto — never dual-send; pick exactly one path.
    explicit_room = str(room_id or "").strip()
    if explicit_room:
        if not has_matrix:
            raise ValueError("指定了 room_id 但未连接 Matrix")
        return ["matrix"]

    channel = get_turn_channel()
    if _is_web_remote_channel(channel):
        if has_web:
            return ["web"]
        if has_matrix:
            # Web turn but only Matrix wired (shouldn't happen once Web registers).
            return ["matrix"]
        raise ValueError("当前 Web 回合无法发送文件：未接线 Web 文件桥")

    if channel is not None or get_turn_channel_id():
        # Matrix / other remote turn — preserve historical phone path.
        if has_matrix:
            return ["matrix"]
        raise ValueError("当前远端回合无法发送文件：未连接 Matrix")

    # CLI / background without remote context — 本地端无文件接收能力，
    # 不兜底投 Matrix/Web，直接判不可用（send_file 的端白名单会更早拦截）。
    raise ValueError("当前端（CLI/本地）无文件接收能力，无法发送文件")


class OutboundFileRouter:
    """Fan-in registration + single-destination dispatch for ``send_file``."""

    def __init__(self) -> None:
        self._matrix: OutboundFileBridge | None = None
        self._web: OutboundFileBridge | None = None

    @property
    def matrix(self) -> OutboundFileBridge | None:
        return self._matrix

    @property
    def web(self) -> OutboundFileBridge | None:
        return self._web

    def set_matrix(self, bridge: OutboundFileBridge | None) -> None:
        """Attach or clear the Matrix bridge without touching the Web bridge."""
        self._matrix = bridge

    def set_web(self, bridge: OutboundFileBridge | None) -> None:
        """Attach or clear the Web bridge without touching the Matrix bridge."""
        self._web = bridge

    async def send_file(
        self,
        path: Path,
        *,
        caption: str = "",
        room_id: str = "",
        target: str = "auto",
        owner: OutboundOwner | None = None,
    ) -> dict[str, Any]:
        """Dispatch one file to exactly one transport.

        ``owner``（发起者归属）随调用透传给桥：Web 桥用它落正确的空间线与端归属，
        Matrix 桥按房间投递（归属由房间决定）。
        """
        destinations = resolve_outbound_targets(
            target,
            has_matrix=self._matrix is not None,
            has_web=self._web is not None,
            room_id=room_id,
        )
        # Exactly one destination for auto / explicit targets today.
        dest = destinations[0]
        if dest == "matrix":
            assert self._matrix is not None
            result = await self._matrix.send_file(path, caption=caption, room_id=room_id, owner=owner)
            result = dict(result)
            result.setdefault("channel", "matrix")
            return result

        assert self._web is not None
        # Web ignores Matrix room_id; do not forward it.
        result = await self._web.send_file(path, caption=caption, room_id="", owner=owner)
        result = dict(result)
        result.setdefault("channel", "web")
        return result


def ensure_outbound_file_router(root: Any) -> OutboundFileRouter:
    """Return the Root-owned router, creating it once."""
    existing = getattr(root, "_outbound_file_router", None)
    if isinstance(existing, OutboundFileRouter):
        return existing
    router = OutboundFileRouter()
    tool_manager = getattr(root, "_tool_manager", None) or getattr(root, "tool_manager", None)
    if tool_manager is not None and "send_file" in getattr(tool_manager, "tools", {}):
        tool_bridge = getattr(tool_manager.tools["send_file"], "_bridge", None)
        if isinstance(tool_bridge, OutboundFileRouter):
            root._outbound_file_router = tool_bridge
            return tool_bridge
    root._outbound_file_router = router
    return router


def register_send_file_on_root(
    root: Any,
    *,
    workspace_root: Path | str,
    router: OutboundFileRouter | None = None,
) -> OutboundFileRouter:
    """Register / refresh ``send_file`` on Root and all WorkspaceSessions."""
    from src.tools.builtin.integration.send_file import SendFileTool

    workspace = Path(workspace_root).resolve()
    active = router or ensure_outbound_file_router(root)
    root._outbound_file_router = active
    root.register_tool(
        SendFileTool(bridge=active, workspace_root=workspace),
        replace=True,
    )
    sessions = getattr(root, "_sessions", None) or {}
    for session in sessions.values():
        coara = getattr(session, "coara", None)
        if coara is None:
            continue
        coara.register_tool(
            SendFileTool(
                bridge=active,
                workspace_root=Path(coara.workspace_dir).resolve(),
            ),
            replace=True,
        )
    return active
