"""工具超时即失败 + 进程树击杀回归：

真实 shell 超时：executor wait_for 到点取消任务 → shell 的 CancelledError
路径 terminate_subprocess_tree → taskkill /T /F 击杀整棵进程树。
孤儿进程是超时语义最大风险点，仅用假工具（await asyncio.Future）覆盖不到
子进程路径：外层 PowerShell 启动一个长睡子进程（到点才写标记文件）并把
子进程 pid 落盘；超时后断言整棵进程树已终止（子进程 pid 不存活、标记文件未写）。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.core.types import ToolCall
from src.tools.builtin.runtime.shell import ShellTool
from tests.helpers import make_test_coara


def _pid_alive(pid: int) -> bool:
    """进程存活探测：Windows 用 OpenProcess/GetExitCodeProcess（os.kill(pid,0)
    在 Windows 上是 TerminateProcess，不能用作探测），POSIX 用 kill(pid,0)。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806
    STILL_ACTIVE = 259  # noqa: N806
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.asyncio
async def test_shell_timeout_kills_process_tree(tmp_path: Path) -> None:
    """真实 shell 超时：取消 → terminate_subprocess_tree → taskkill /T /F 击杀进程树。"""
    if os.name != "nt":
        pytest.skip("本用例依赖 Windows PowerShell 进程树语义")
    coara = make_test_coara(tmp_path)
    coara.register_tool(ShellTool())
    await coara.initialize()

    marker = tmp_path / "marker.txt"
    child_pid_file = tmp_path / "child.pid"
    cmd = (
        "$child = Start-Process -FilePath 'powershell.exe' -ArgumentList "
        "'-NoProfile','-Command',\"Start-Sleep -Seconds 60; "
        f"Set-Content -LiteralPath '{marker}' -Value done\" -PassThru -NoNewWindow; "
        f"$child.Id | Set-Content -LiteralPath '{child_pid_file}' -Encoding ascii; "
        "Start-Sleep -Seconds 60"
    )

    # timeout_ms=5000 → 前台超时 5s 即失败，给外层 PS 留足时间落盘子进程 pid
    executor = ToolExecutor(default_timeout=120.0)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-kill", name="shell", arguments={"command": cmd, "timeout_ms": 5000})],
        is_owner=True,
    )
    result = executions[0].result
    assert result.is_error
    assert "timed out" in (result.content or "").lower()

    # 子进程 pid 应已落盘
    deadline = time.monotonic() + 15
    while not child_pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert child_pid_file.exists(), "外层命令未落盘子进程 pid"
    child_pid = int(child_pid_file.read_text(encoding="ascii").strip())

    # 进程树已终止：子进程不存活、标记文件未写
    deadline = time.monotonic() + 15
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _pid_alive(child_pid), f"子进程 {child_pid} 仍存活，进程树未被击杀"
    assert not marker.exists(), "标记文件被写入：进程树未被击杀"
