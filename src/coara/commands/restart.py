"""``/restart``：静默重启内核。

静默契约：成功路径不产出任何端上可见输出——内核确认新实例健康后即退出，端下次连接
自然连到新实例，不需要被告知。唯一会出现在端上的是失败原因：那时旧进程仍在运行，
必须让发起者知道。

协议见 ``src/runtime/restart.py``（落意图 → 同模式拉起 → 健康确认 → 成功即退出；
失败即回退并保留原进程）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult


@register("restart")
async def handle_restart(root: Any, args: CommandArgs) -> CommandResult:
    """重启内核。成功路径不会返回（内核已退出）；失败则把原因回报给发起端。"""
    del args
    from src.runtime.restart import restart_kernel

    workspace_manager = getattr(root, "workspace_manager", None)
    home = getattr(workspace_manager, "coara_home", None)
    # 成功路径在 restart_kernel 内 os._exit(0) 直接交棒，到不了这一行——命令层因此
    # 天然静默（不推 command_result、不落帧）。只有失败才走到下面，返回可见错误：
    # 那时旧进程仍在运行，必须让发起者知道。
    _ok, detail = await restart_kernel(
        reason="manual", requested_by="owner", root=root, home=Path(home) if home else None
    )
    return CommandResult(output=f"重启失败，仍在原进程运行：{detail}", data={"error": True})
