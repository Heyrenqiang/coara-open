"""后台工作空间工具行打印通道（多工作空间统一 transcript）"""

from __future__ import annotations

from typing import Any

from src.cli.activity_live import ActivityLiveTracker
from src.cli.activity_types import (
    _NON_TOOL_ACTIVITY_KINDS,
    _ROOT_TOOL_KINDS_FLUSH_SKIP,
    ToolCallBlock,
    merge_adjacent_tool_blocks,
)
from src.cli.scrollback import CliScrollback
from src.cli.workspace_activity import workspace_color

# tool_call_id -> 空间映射的防泄漏上限：tool_start 后 complete 丢失时裁最旧
_TOOL_WS_MAP_CAP = 512


class _BackgroundActivityTracker(ActivityLiveTracker):
    """与前台唯一差异：主会话工具（depth 0）也允许 flush。"""

    def _should_flush(self, block: ToolCallBlock) -> bool:
        if block.advisor_suppressed:
            return False
        if block.tool_name in _ROOT_TOOL_KINDS_FLUSH_SKIP and block.depth <= 1:
            return False
        return block.tool_name not in _NON_TOOL_ACTIVITY_KINDS


class BackgroundToolHistory:
    """后台工作空间工具行的收集与打印（事件驱动，无渲染循环介入）。"""

    def __init__(self, root: Any) -> None:
        self._root = root
        self._tracker = _BackgroundActivityTracker()
        # tool_call_id -> 工作空间目录；目录 -> (空间名, 暗色)
        self._ws_by_tool: dict[str, str] = {}
        self._ws_info: dict[str, tuple[str, str]] = {}
        # 系统静默子智能体（janitor/daily）的 tool_call_id：其工具行不显示
        self._silent_tool_ids: set[str] = set()

    def ingest(self, event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        if event.event_type == "tool_start":
            ws = str(payload.get("workspace_dir") or "").strip()
            if tool_call_id and ws:
                if len(self._ws_by_tool) >= _TOOL_WS_MAP_CAP:
                    for stale in list(self._ws_by_tool)[: _TOOL_WS_MAP_CAP // 2]:
                        self._ws_by_tool.pop(stale, None)
                from src.coara.turn_detach import workspace_display_name

                _bright, dim = workspace_color(ws)
                self._ws_by_tool[tool_call_id] = ws
                self._ws_info.setdefault(ws, (workspace_display_name(self._root, ws), dim))
        elif event.event_type == "tool_complete" and bool(payload.get("cli_silent", False)):
            # cli_silent 只盖在 tool_complete 上；subagent_start 缺失时
            # tracker 的子树抑制兜不住，这里记 id 在 flush 时丢弃
            if tool_call_id:
                if len(self._silent_tool_ids) >= _TOOL_WS_MAP_CAP:
                    # 防泄漏兜底：被 tracker 抑制规则丢弃的块不会经过 flush 清理
                    self._silent_tool_ids = set(list(self._silent_tool_ids)[-_TOOL_WS_MAP_CAP // 2 :])
                self._silent_tool_ids.add(tool_call_id)
        self._tracker.ingest(event)

    def flush(self) -> bool:
        """打印已完成工具行：按空间分组后再做相邻同类合并。

        返回是否有内容落盘（收尾链据此判断是否需要补终帧）。
        """
        try:
            blocks = self._tracker.flush_finished_blocks(merge=False)
        except Exception as exc:  # 打印失败不得打断事件循环
            from src.core.logger import logger

            logger.warning("Background tool history flush failed: {}", exc)
            return False
        if not blocks:
            return False
        groups: dict[str, list[ToolCallBlock]] = {}
        order: list[str] = []
        for block in blocks:
            key = self._ws_by_tool.get(block.tool_call_id, "")
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(block)
        # 本批工具已收尾，映射随手清理（合并行保留首块 id，也在本批内）
        for block in blocks:
            self._ws_by_tool.pop(block.tool_call_id, None)
        try:
            for key in order:
                name, dim = self._ws_info.get(key, ("", ""))
                for block in merge_adjacent_tool_blocks(groups[key]):
                    if block.tool_call_id in self._silent_tool_ids:
                        self._silent_tool_ids.discard(block.tool_call_id)
                        continue
                    self._print_block(block, name=name, dim=dim)
        except Exception as exc:
            from src.core.logger import logger

            logger.warning("Background tool history print failed: {}", exc)
        return True

    def _print_block(self, block: ToolCallBlock, *, name: str, dim: str) -> None:
        from src.cli.streaming import write_tool_history_html
        from src.cli.theme import get_tool_error_style, get_tool_text_style

        tag = self._tracker.subagent_tag_for_block(block)
        line = CliScrollback.esc(ActivityLiveTracker.format_history_line(block, subagent_tag=tag))
        prefix = f"[{CliScrollback.esc(name)}] " if name else ""
        if block.is_error:
            err = get_tool_error_style()
            write_tool_history_html(f'<style fg="{err}">{prefix}{line}</style>')
        elif dim:
            write_tool_history_html(f'<style fg="{dim}">{prefix}{line}</style>')
        else:
            tool = get_tool_text_style()
            write_tool_history_html(f'<style fg="{tool}">{prefix}{line}</style>')
