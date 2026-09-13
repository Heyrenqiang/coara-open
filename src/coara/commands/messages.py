"""``/message`` — 查看全局系统消息历史。

系统消息与工作空间动态（``/updates`` / ``ws updates_*``）是两套东西：
系统消息面向用户本人的环境提示（如「API key 未配置」），跨空间、无红点语义，
只在用户主动打开本命令时呈现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@register("message")
@register("messages")
async def handle_message(root: RootCoara, args: CommandArgs) -> CommandResult:
    from src.core.system_messages import list_system_messages

    coara_home = getattr(root.workspace_manager, "coara_home", None)
    if not coara_home:
        return CommandResult.error("无法定位 coara_home，无法读取系统消息")

    limit = 50
    if args.value and args.value.isdigit():
        limit = min(int(args.value), 200)

    messages = list_system_messages(coara_home, limit=limit)
    if not messages:
        return CommandResult.text("暂无系统消息", messages=[])

    lines = [f"系统消息（最近 {len(messages)} 条，新的在后）："]
    for m in messages:
        ts = str(m.get("ts") or "")
        title = str(m.get("title") or "")
        body = str(m.get("body") or "")
        lines.append(f"· {title}" + (f"  [{ts}]" if ts else ""))
        if body:
            lines.append(body)
        lines.append("")
    return CommandResult.text("\n".join(lines).rstrip(), messages=messages)
