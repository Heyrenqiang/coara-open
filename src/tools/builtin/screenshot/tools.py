"""屏幕捕捉工具（内置挂起，tool(activate) 揭示后可用）。

截图是单帧捕捉，录制是连续捕捉，同族不改环境：
- 截图/压缩能力源自退役的 MCP screenshot server，语义保持一致：返回保存后的文件路径。
- 录制是 standalone/makevideo 引擎的薄适配，会话落在工作空间 .coara/make_video/sessions/。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

from . import record, screenshot

_ACTIONS: dict[str, tuple[Any, str, tuple[str, ...]]] = {
    "fullscreen": (screenshot.capture_fullscreen, "截取全屏", ()),
    "region": (screenshot.capture_region, "区域截图", ("x", "y", "width", "height")),
    "compress": (screenshot.compress_image, "压缩图片", ("image_path", "quality")),
    "record_start": (record.record_start, "开录屏幕", ("fps", "area")),
    "record_stop": (record.record_stop, "停录屏幕", ()),
    "record_status": (record.record_status, "录制状态", ()),
}

_RECORD_ACTIONS = frozenset({"record_start", "record_stop", "record_status"})


class _Invocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], workspace_root: Path | None = None):
        super().__init__(params)
        self._workspace_root = workspace_root

    def get_description(self) -> str:
        action = str(self.params.get("action") or "")
        return _ACTIONS.get(action, (None, "屏幕捕捉", ()))[1]

    async def execute(self, signal=None) -> ToolResult:
        action = str(self.params.get("action") or "")
        entry = _ACTIONS.get(action)
        if entry is None:
            return ToolResult.error(f"未知 action：{action!r}，可用 {sorted(_ACTIONS)}")
        fn, _, keys = entry
        kwargs = {k: self.params[k] for k in keys if k in self.params}
        if action in _RECORD_ACTIONS:
            kwargs["workspace"] = self._workspace_root or Path.cwd()
        try:
            result = await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(str(result))


class ScreenshotTool(BaseTool):
    name = "screenshot"
    summary = "屏幕捕捉，截图、压缩、录制"
    description = (
        "屏幕捕捉，截图/压缩/录制；截图保存/返回文件路径，可用 read 查看图片内容；"
        "录制为连续捕捉，原片落在工作空间 .coara/make_video/sessions/，"
        "voice 留底在停录时自动混流进音轨。\n\n"
        "| action | 用途 |\n"
        "|--------|------|\n"
        "| `fullscreen` | 截取所有显示器的完整屏幕，保存为 PNG |\n"
        "| `region` | 截取指定区域（需 x/y/width/height），保存为 PNG |\n"
        "| `compress` | 将图片压缩为 JPEG（减小体积，便于发送/上传），需 image_path，quality 默认 70 |\n"
        '| `record_start` | 开录屏幕（连续捕捉，fps 默认 30，area 形如 "x,y,w,h" 缺省主屏全屏） |\n'
        "| `record_stop` | 停录并收尾出原片，voice 留底自动混流 |\n"
        "| `record_status` | 查询是否正在录制 |"
    )
    display_name = "Screenshot"
    category = "system"
    kind = ToolKind.EXECUTE
    should_defer = True
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["fullscreen", "region", "compress", "record_start", "record_stop", "record_status"],
                "description": "fullscreen=全屏截图；region=区域截图；compress=压缩图片；"
                "record_start=开录；record_stop=停录；record_status=录制状态",
            },
            "x": {"type": "integer", "description": "region 的左上角横坐标"},
            "y": {"type": "integer", "description": "region 的左上角纵坐标"},
            "width": {"type": "integer", "description": "region 的宽度（像素）"},
            "height": {"type": "integer", "description": "region 的高度（像素）"},
            "image_path": {"type": "string", "description": "compress 的原图绝对路径"},
            "quality": {"type": "integer", "description": "compress 的 JPEG 质量 1-100，默认 70", "default": 70},
            "fps": {"type": "integer", "description": "record_start 的帧率，默认 30", "default": 30},
            "area": {"type": "string", "description": 'record_start 的录制区域 "x,y,w,h"，缺省主屏全屏'},
        },
        "required": ["action"],
    }

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root
        super().__init__()

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _Invocation(params, workspace_root=self._workspace_root)
