"""跨平台子进程原语（core 层，零上层依赖）.

后台执行器（background/bash_runner）与 shell 工具（tools/builtin/runtime）共用的
进程解码与 PowerShell 生成逻辑。只依赖标准库，可被任意下层 import。
"""

from __future__ import annotations

import base64
import os
import shutil
from collections.abc import Callable
from typing import Any


def decode_subprocess_output(data: bytes) -> str:
    """Decode captured stdout/stderr: UTF-8 with replacement for invalid bytes."""
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def resolve_windows_powershell() -> str | None:
    if os.name != "nt":
        return None
    return shutil.which("pwsh.exe") or shutil.which("powershell.exe")


def powershell_encoded_argv(command: str) -> list[str]:
    # Force UTF-8 console/pipeline encoding so captured stdout decodes reliably.
    ps_script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[Console]::OutputEncoding;" + command + ";exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    return ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def build_powershell_exec_cmd(command: str) -> tuple[str, list[str]] | None:
    """Return (executable, argv) for asyncio.create_subprocess_exec on Windows."""
    ps_path = resolve_windows_powershell()
    if not ps_path:
        return None
    return ps_path, powershell_encoded_argv(command)


def schedule_threadsafe(loop: Any, fn: Callable[[], None]) -> None:
    """Schedule *fn* on *loop* via ``call_soon_threadsafe``; call directly when none runs.

    Shared by CLI remote-event consumers (RemoteEventBus / RootShim): WS 帧可能在
    非事件循环上下文到达，需切回绑定的事件循环；单线程测试无运行循环时退化为
    直接同步调用。
    """
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(fn)
    else:
        fn()
