"""``/restart``：重启内核（supervisor 托管下为优雅退 + exit 42，由 supervisor respawn）"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult


@register("restart")
async def handle_restart(root: Any, args: CommandArgs) -> CommandResult:
    """重启内核。仅 supervisor 托管下可用；触发优雅收尾后以 EXIT_RESTART 退出。

    非托管形态（开发直跑 python -m src.coara）明确拒绝——没有 supervisor 在
    外面等着 respawn，自杀就是真死。
    """
    del args
    if os.environ.get("COARA_SUPERVISED") != "1":
        return CommandResult(
            output="当前内核未被托管，无法自重启。请直接结束进程后重新启动。",
            data={"error": True},
        )

    from src.runtime.restart import request_restart

    workspace_manager = getattr(root, "workspace_manager", None)
    home = getattr(workspace_manager, "coara_home", None)
    request_restart(root, reason="manual", requested_by="owner", home=Path(home) if home else None)
    return CommandResult(
        output="正在重启考拉…断线几秒后自动重连",
        action="restart",
        data={"restarting": True},
    )
