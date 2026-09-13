"""Send a local file to Matrix and/or the Web UI via OutboundFileRouter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.tools.builtin.file_io.file_support import resolve_workspace_path_from_root
from src.tools.builtin.integration.outbound_file import OutboundFileRouter, OutboundOwner


class SendFileTool(BaseTool):
    name = "send_file"
    description = """将文件发送给用户（手机端 或 Web端）

注意事项
- 只在用户有需要的时候发，但是用户在手机端或web端对话时，要求生成某某内容的时候，一般都有需要
- 工作区内外的本地文件都直接发送，`path` 传绝对路径
- `target` 默认 `auto`，Web 回合发到浏览器，Matrix/手机回合发到房间；也可显式 `web` / `matrix`
- 发哪一端由「本条用户消息来自哪一端」决定，不得跨端：本轮来自手机就发手机，来自 Web 就发 Web；
  某一端发不出去，例如手机端单文件上限 20MB，就如实告诉用户，不要改发另一端
- 手机端（Matrix）单文件上限 20MB，超过必被拒；给手机发大文件前先确认文件大小
"""
    display_name = "Send File"
    category = "system"
    kind = ToolKind.EXECUTE
    owner_only = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要发送文件的绝对路径，工作区内外均可",
            },
            "caption": {
                "type": "string",
                "description": "随文件附带的说明文字，可选",
            },
            "room_id": {
                "type": "string",
                "description": "Matrix 房间 ID，可选；传入则强制发到手机端",
            },
            "target": {
                "type": "string",
                "enum": ["auto", "matrix", "web"],
                "description": "发送目标，auto 按当前回合路由；matrix=手机；web=浏览器",
            },
        },
        "required": ["path"],
    }

    def __init__(self, *, bridge: OutboundFileRouter, workspace_root: Path):
        super().__init__()
        self._bridge = bridge
        self._workspace_root = workspace_root.resolve()

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return SendFileInvocation(params, bridge=self._bridge, workspace_root=self._workspace_root)

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        return None

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        # 单点副作用，串行
        return "send_file"


class SendFileInvocation(ToolInvocation):
    # 仅远端（手机/web）回合可用；CLI/后台/事件等本地端禁止 send_file。
    _ALLOWED_SOURCES = frozenset({"web", "matrix"})

    def __init__(
        self,
        params: dict[str, Any],
        *,
        bridge: OutboundFileRouter,
        workspace_root: Path,
    ):
        super().__init__(params)
        raw_path = params.get("path")
        if not raw_path or not isinstance(raw_path, str):
            raise ValueError("path is required")
        self.caption = str(params.get("caption") or "")
        self.room_id = str(params.get("room_id") or "")
        self.target = str(params.get("target") or "auto").strip().lower() or "auto"
        self._bridge = bridge
        self._workspace_root = workspace_root
        self.path = self._resolve_path(raw_path)

    def get_description(self) -> str:
        return f"Send file: {self.path.name}"

    async def execute(self, signal=None) -> ToolResult:
        if signal is not None and getattr(signal, "aborted", False):
            return ToolResult.cancelled()
        source = self._current_turn_source()
        if source not in self._ALLOWED_SOURCES:
            return ToolResult.error(
                f"send_file 仅手机端 / Web 端可用，当前端（{source or '本地'}）不支持；请直接以文本回复文件路径。"
            )
        # 端绑定：只发到本条用户消息来的端，不允许显式跨端（用户明确要求：不能乱发）。
        if self.target in self._ALLOWED_SOURCES and self.target != source:
            here = "手机端" if source == "matrix" else "Web 端"
            return ToolResult.error(
                f"send_file 只能发到本条用户消息来的端（当前为{here}），不允许跨端发送。"
                "目标端发不出去就如实告诉用户，不要改发另一端口。"
            )
        try:
            result = await self._bridge.send_file(
                self.path,
                caption=self.caption,
                room_id=self.room_id,
                target=self.target,
                owner=self._owner(),
            )
        except Exception as exc:
            return ToolResult.error(f"发送文件失败：{exc}")
        channel = str(result.get("channel") or "")
        where = {"matrix": "手机端", "web": "Web 端"}.get(channel, "远端")
        return ToolResult.success(
            f"已发送文件到{where}：{result.get('filename', self.path.name)}",
            metadata=result,
        )

    def _current_turn_source(self) -> str:
        """当前回合端（取自 EndChannel ContextVar）；无端信息时返回空串（视为本地）。"""
        from src.coara.turn_context import get_end_channel

        end = get_end_channel()
        return str(getattr(end, "source", "") or "").strip().lower()

    def _owner(self) -> OutboundOwner:
        """发起者归属：本次调用的空间/会话/回合（executor 经 bind_runtime_context 注入）。

        绝不用端视图推——投递可能异步完成（视频等几分钟），期间用户换了空间，
        按视图推就会把产出写进别人的线。缺归属时留空，由 Web 桥拒绝落带。
        """
        workspace_dir = str(getattr(self, "workspace_dir", "") or "") or str(self._workspace_root or "")
        return OutboundOwner(
            workspace_dir=workspace_dir,
            session_id=str(getattr(self, "session_id", "") or ""),
            turn_id=str(getattr(self, "turn_id", "") or ""),
        )

    def _resolve_path(self, raw_path: str) -> Path:
        candidate = str(raw_path or "").strip()
        path, _, path_error = resolve_workspace_path_from_root(
            candidate,
            self._workspace_root,
            "send_file",
        )
        if path is not None and path_error is None:
            if not path.exists() or not path.is_file():
                raise ValueError(f"文件不存在或不是普通文件：{path}")
            return path

        # 工作区外的绝对路径同样允许；宝箱路径按门禁规则处理（sealed 拒绝，open/ 解锁后放行）
        from src.vault.guard import vault_path_denied

        denied = vault_path_denied(candidate)
        if denied:
            raise ValueError(denied)
        explicit = Path(candidate).expanduser()
        if explicit.is_absolute():
            resolved = explicit.resolve()
            if resolved.is_file():
                return resolved
            raise ValueError(f"文件不存在或不是普通文件：{candidate}")
        raise ValueError("send_file 需要文件的绝对路径。")
