"""Tool output pipeline — Model / Terminal / Gate channel routing.

See ``docs/TOOL_OUTPUT_FRAMEWORK.md``.
"""

from __future__ import annotations

from typing import Any

from src.coara.tool_output.types import DiffDisplayBlock


def serialize_display_blocks(blocks: list[Any] | None) -> list[dict[str, Any]] | None:
    if not blocks:
        return None
    serialized: list[dict[str, Any]] = []
    for item in blocks:
        if isinstance(item, DiffDisplayBlock):
            serialized.append(item.to_dict())
        elif isinstance(item, dict):
            serialized.append(dict(item))
    return serialized or None


def deserialize_display_blocks(payload: list[dict[str, Any]] | None) -> list[DiffDisplayBlock]:
    if not payload:
        return []
    return [DiffDisplayBlock.from_dict(item) for item in payload if isinstance(item, dict)]


def render_terminal_blocks(
    blocks: list[Any],
    *,
    title_prefix: str = "",
    head_style: str | None = None,
    compact: bool = False,
) -> None:
    """Paint display blocks to CLI scrollback (Terminal channel).

    ``title_prefix``/``head_style``/``compact`` 区分后台工作空间（头线带
    [空间] 前缀染空间色、行数预算收紧）；前台全部走默认值。
    """
    if not blocks:
        return
    try:
        from src.coara.diff_render import render_display_blocks
        from src.coara.frontend import get_frontend

        view = render_display_blocks(
            blocks,
            preview=False,
            title_prefix=title_prefix,
            head_style=head_style,
            compact=compact,
        )
        if view is None:
            return
        # diff 紧跟 ✓ 工具行（之间不留空行）；块尾空行与后续内容分隔，
        # 并同步活跃 StreamingBlock 状态——该空行充当后续文本块的块首空行，
        # 避免 append 块首逻辑再写一个叠加成两个空行
        get_frontend().scrollback_write_renderable(view)
        get_frontend().scrollback_write("")
        get_frontend().notify_block_rendered_after_tool_line()
    except Exception as exc:
        from src.core.logger import logger

        logger.warning("Failed to render tool display blocks to CLI: {}", exc)


def render_terminal_from_event(
    *,
    is_error: bool,
    display_blocks: list[dict[str, Any]] | None,
    title_prefix: str = "",
    head_style: str | None = None,
    compact: bool = False,
) -> None:
    """EventBus ``tool_complete`` handler entry."""
    if is_error or not display_blocks:
        return
    render_terminal_blocks(
        deserialize_display_blocks(display_blocks),
        title_prefix=title_prefix,
        head_style=head_style,
        compact=compact,
    )


async def render_gate_preview(invocation: Any):
    """Before confirm: compact diff for edit."""
    from src.coara.diff_render import render_display_blocks
    from src.coara.tool_output.gate_preview import build_gate_preview_blocks

    blocks = await build_gate_preview_blocks(invocation)
    if not blocks:
        return None
    return render_display_blocks(blocks, preview=True)
