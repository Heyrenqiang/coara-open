"""Shared types/constants for CLI activity display."""

from __future__ import annotations

from dataclasses import dataclass

_NON_TOOL_ACTIVITY_KINDS = frozenset({"subagent", "background", "workflow", "process"})
_ROOT_TOOL_KINDS_FLUSH_SKIP = frozenset({"delegate"} | _NON_TOOL_ACTIVITY_KINDS)
MAX_STATUS_TOTAL_LINES = 12
MAX_STATUS_OVERFLOW_HINT = 11
MAX_ACTIVE_TOOLS_PER_DELEGATE = 4

# 可合并的调研类只读工具：连续同类执行时归并成一条，降低滚动区噪声
# （信息不丢：/log 从录像带 tool/exec 事件可逐个展开完整输出）
_MERGEABLE_TOOLS = frozenset({"read", "glob", "grep"})


@dataclass(slots=True)
class ToolCallBlock:
    """Finished tool row for scrollback flush."""

    tool_call_id: str
    tool_name: str
    label: str
    is_error: bool = False
    finished: bool = False
    depth: int = 0
    advisor_suppressed: bool = False
    # 所属子智能体运行的容器 id（delegate 行 call_id / 后台 task_id）；空串＝主会话工具行。
    # 在节点回收前（_finish 时）算好带上——节点树会被 prune，事后查不到祖先。
    owner_id: str = ""


def should_flush_tool_to_history(block: ToolCallBlock) -> bool:
    """Subagent tools flush immediately; root tools stay on turn_orchestrator yield."""
    if block.depth <= 0:
        return False
    if block.tool_name in _ROOT_TOOL_KINDS_FLUSH_SKIP and block.depth <= 1:
        return False
    return block.tool_name not in _NON_TOOL_ACTIVITY_KINDS


def merge_adjacent_tool_blocks(blocks: list[ToolCallBlock]) -> list[ToolCallBlock]:
    """把相邻同类的只读工具块合并成一条（完成态批量打印时归并噪声）。

    合并条件：连续、同 depth、同类（read/glob/grep）、非错误。其余原样。
    合并行保留首个块的标识；每个工具的完整执行记录仍在录像带
    （tool/exec 事件），需要时可按 tool_call_id 定位（/log --tool）。
    """
    if not blocks:
        return blocks

    def _flush_group(group: list[ToolCallBlock], out: list[ToolCallBlock]) -> None:
        if len(group) >= 2:
            first = group[0]
            out.append(
                ToolCallBlock(
                    tool_call_id=first.tool_call_id,
                    tool_name=first.tool_name,
                    label=f"{first.tool_name} ×{len(group)}",
                    is_error=False,
                    finished=True,
                    depth=first.depth,
                    advisor_suppressed=first.advisor_suppressed,
                    owner_id=first.owner_id,
                )
            )
        else:
            out.extend(group)

    out: list[ToolCallBlock] = []
    group: list[ToolCallBlock] = []
    for block in blocks:
        mergeable = block.tool_name in _MERGEABLE_TOOLS and not block.is_error and block.finished
        if group and mergeable and block.tool_name == group[-1].tool_name and block.depth == group[-1].depth:
            group.append(block)
            continue
        _flush_group(group, out)
        group = [block] if mergeable else []
        if not mergeable:
            out.append(block)
    _flush_group(group, out)
    return out
