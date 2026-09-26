"""Web handler mixin 的共享宿主契约声明（``src/ui`` 域）"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any


class HandlerMixinBase:
    """声明 handler mixin 组合后由宿主提供的共享成员（仅类型层面，运行期空类）。"""

    if TYPE_CHECKING:
        root: Any
        registry: Any
        attach_registry: Any
        attach_interaction_channel: Any
        trace_store: Any
        _web_file_bridge: Any
        _current_runtime: Any

        coara_home: Path | None
        workspace_dir: Path
        _module_roots: dict[str, Any]
        skip_trace_persistence: bool
        _MAX_TEXT_FILE_CHARS: int
        _FALLBACK_RAW_IMAGE_MAX_BYTES: int

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

        _turns: Any
        _chat_tasks: Any
        _subscriptions: Any
        _trace_batch_lock: Any
        _web_followup_view_turns: set[tuple[str, str]]


__all__ = ["HandlerMixinBase"]
