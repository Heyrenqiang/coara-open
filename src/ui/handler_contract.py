"""Web handler mixin 的共享宿主契约声明（``src/ui`` 域）。

``WebServer(*HANDLER_MIXINS)`` 把十余个按域拆分的 handler mixin 组合成一个类，
但每个 mixin 单独看都缺少宿主提供的成员——``self.root``、``self._check_token``、
``self.coara_home`` 等由兄弟 mixin 或 ``WebServer`` 本体提供。此前这份契约只以
``attach_ws.py`` 等模块 docstring 里的「隐式契约」文字存在。

本模块把它变成机器可校验的声明：``HandlerMixinBase`` 里的属性**全部包在
``if TYPE_CHECKING:`` 内**，运行期该类不含任何成员（``__annotations__`` 为空、
无属性、无方法），因此子类继承它**不产生任何运行期行为差异**，仅在类型检查器
眼中补齐「组合后才会存在」的成员。

放在 ``src/ui/`` 而非 ``src/ui/handlers/`` 是有意的：``handlers/__init__.py`` 会
反向导入 ``src.ui.attach_ws``，契约模块若落在 ``handlers`` 包内会与 ``attach_ws``
形成导入环。

新增成员时的约定：
- 宿主（``WebServer``）真正提供的数据属性 → 按实际类型声明；难以精确表达时用 ``Any``。
- 宿主/兄弟 mixin 提供的方法 → 声明为 ``Callable[..., Any]``，保持调用点可校验。
- 只在本 mixin 内定义、不跨 mixin 使用的成员**不要**放进来，避免掩盖真实拼写错误。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any


class HandlerMixinBase:
    """声明 handler mixin 组合后由宿主提供的共享成员（仅类型层面，运行期空类）。"""

    if TYPE_CHECKING:
        # —— 宿主核心对象 ——
        root: Any
        registry: Any
        attach_registry: Any
        attach_interaction_channel: Any
        trace_store: Any
        _web_file_bridge: Any
        _current_runtime: Any

        # —— 路径与配置 ——（coara_home 允许为空：无 home 的独立 handler 实例为 None）
        coara_home: Path | None
        workspace_dir: Path
        _module_roots: dict[str, Any]
        skip_trace_persistence: bool
        _MAX_TEXT_FILE_CHARS: int
        _FALLBACK_RAW_IMAGE_MAX_BYTES: int

        # —— 宿主提供的处理方法（Callable 保证调用点可校验，不求签名精确）——
        _check_token: Callable[..., Any]
        _send_error: Callable[..., Any]
        _view_coara: Callable[..., Any]
        _spawn_bg_task: Callable[..., Any]
        _gc_finished_turns: Callable[..., Any]
        _build_lightweight_runtime: Callable[..., Any]
        _is_module_subject: Callable[..., Any]
        _get_flow_root: Callable[..., Any]
        _normalize_tool_ws_fields: Callable[..., Any]
        _bind_view_store: Callable[..., Any]
        _load_view_snapshot: Callable[..., Any]
        _persist_outbound_file: Callable[..., Any]
        _resolve_file_target: Callable[..., Any]
        _persist_timeline_divider: Callable[..., Any]

        # —— 会话与回合状态容器 ——
        _turns: Any
        _chat_tasks: Any
        _subscriptions: Any
        _trace_batch_lock: Any
        _web_followup_view_turns: set[tuple[str, str]]


__all__ = ["HandlerMixinBase"]
