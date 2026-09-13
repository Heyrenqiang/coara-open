"""具身器官工具（内置挂起，tool(activate) 揭示后可用）。

手 actuate / 嘴巴 voice / 字 subtitle / 章节 mark——调用即真实发生，
引擎在 standalone/makevideo，本层只做工作空间绑定与参数分发。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

from . import organ

_ACTUATE_KEYS = (
    "kind",
    "x",
    "y",
    "text",
    "combo",
    "button",
    "n",
    "x1",
    "y1",
    "x2",
    "y2",
    "duration",
    "points",
    "spacing",
    "easing",
    "title",
    "to",
    "sec",
    "note",
)

_ACTIONS: dict[str, tuple[Any, str, tuple[str, ...]]] = {
    "actuate": (organ.actuate_run, "手·真实键鼠", _ACTUATE_KEYS),
    "voice": (organ.voice_say, "嘴巴·放声", ("text", "engine", "speaker", "caption", "wait")),
    "mark": (organ.mark_scene, "章节标记", ("scene",)),
}

_NO_WORKSPACE: frozenset[str] = frozenset()


class _Invocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], workspace_root: Path | None = None):
        super().__init__(params)
        self._workspace_root = workspace_root

    def get_description(self) -> str:
        action = str(self.params.get("action") or "")
        return _ACTIONS.get(action, (None, "具身器官", ()))[1]

    async def execute(self, signal=None) -> ToolResult:
        action = str(self.params.get("action") or "")
        entry = _ACTIONS.get(action)
        if entry is None:
            return ToolResult.error(f"未知 action：{action!r}，可用 {sorted(_ACTIONS)}")
        fn, _, keys = entry
        kwargs = {k: self.params[k] for k in keys if k in self.params}
        if action not in _NO_WORKSPACE:
            kwargs["workspace"] = self._workspace_root or Path.cwd()
        try:
            result = await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(str(result))


class EmbodyTool(BaseTool):
    name = "embody"
    summary = "具身器官，真实键鼠操作与放声，仅用户可见"
    description = (
        "具身器官（手/嘴巴），调用即真实发生，actuate 真实键鼠操作桌面、"
        "voice 在电脑真实放声（edge 在线自然音，离线回退 SAPI）并默认同文字幕协同烧上屏"
        "（底部居中浮层，说完即隐，被录制自然入镜；caption=false 关闭字幕）。自导自演演示用，"
        "配 screenshot 的 record_start/record_stop 开收录制；"
        "录制中器官调用自动写入动作时间轴，voice 放声同时留底、停录自动混流进原片音轨。\n\n"
        "| action | 用途 |\n"
        "|--------|------|\n"
        "| `actuate` | 手，kind=click/move/type/key/scroll/drag/stroke/wait/pos/focus + 对应参数；"
        "点击前先 focus 目标窗口再截图确认，避免误点；"
        "stroke 用笔触路径 points 画连续线条，绘画/圈选用，一笔不断 |\n"
        "| `voice` | 嘴巴，调用即放声讲完 text 并同步烧字（默认阻塞到说完；边说边做传 wait=false 后台讲） |\n"
        "| `mark` | 章节边界写进动作时间轴，需录制中，便于回看定位 |"
    )
    display_name = "Embody"
    category = "system"
    kind = ToolKind.EXECUTE
    should_defer = True
    owner_only = True  # 真实键鼠/放声：访客（untrusted）不可见不可调
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["actuate", "voice", "mark"],
                "description": "actuate=真实键鼠；voice=放声，默认同文字幕协同；mark=章节",
            },
            "kind": {
                "type": "string",
                "enum": ["click", "move", "type", "key", "scroll", "drag", "stroke", "wait", "pos", "focus"],
                "description": "actuate 的动作类型",
            },
            "x": {"type": "integer", "description": "actuate click/move 的横坐标"},
            "y": {"type": "integer", "description": "actuate click/move 的纵坐标"},
            "button": {"type": "string", "description": "actuate click 取 left/right，默认 left"},
            "text": {"type": "string", "description": "actuate type=输入文本，剪贴板粘贴；voice=讲的话"},
            "combo": {"type": "string", "description": "actuate key 的按键或组合键，如 enter / ctrl+s"},
            "n": {"type": "integer", "description": "actuate scroll 的滚动量，正上负下"},
            "x1": {"type": "integer", "description": "actuate drag 的起点横坐标"},
            "y1": {"type": "integer", "description": "actuate drag 的起点纵坐标"},
            "x2": {"type": "integer", "description": "actuate drag 的终点横坐标"},
            "y2": {"type": "integer", "description": "actuate drag 的终点纵坐标"},
            "duration": {"type": "number", "description": "actuate move/drag/stroke 的耗时秒，缺省按距离自适应"},
            "points": {
                "type": "string",
                "description": (
                    'actuate stroke 的笔触路径，JSON 数组 "[[x,y],[x,y],...]" 或简写 "x,y;x,y"；'
                    "给 5~20 个路径骨架点即可，引擎自动致密插值成连续笔触"
                ),
            },
            "spacing": {"type": "number", "description": "actuate stroke 的插值像素间距，默认 3，越小越细腻"},
            "easing": {
                "type": "boolean",
                "description": "actuate stroke 的起笔收笔缓动（笔锋），默认 true",
                "default": True,
            },
            "title": {"type": "string", "description": "actuate focus 的目标窗口标题，包含匹配"},
            "to": {"type": "string", "description": 'actuate focus 的窗口归位坐标 "x,y"'},
            "sec": {"type": "number", "description": "actuate wait 的等待秒数"},
            "note": {"type": "string", "description": "actuate 的备注，只进时间轴"},
            "engine": {
                "type": "string",
                "enum": ["edge", "sapi"],
                "description": "voice 的引擎，默认 edge（在线）",
                "default": "edge",
            },
            "speaker": {"type": "string", "description": "voice 的 edge 音色，默认 zh-CN-XiaoxiaoNeural"},
            "caption": {"type": "boolean", "description": "voice 是否同步烧同文字幕，默认 true", "default": True},
            "wait": {
                "type": "boolean",
                "description": "voice 默认 true 阻塞到说完；false 后台讲，边说边做",
                "default": True,
            },
            "scene": {"type": "string", "description": "mark 的章节名"},
        },
        "required": ["action"],
    }

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root
        super().__init__()

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _Invocation(params, workspace_root=self._workspace_root)
