"""Windows 子进程创建标志 —— 隐藏控制台窗口。

Windows 下父进程无控制台（GUI 启动、服务、后台/无 TTY 环境）时，
asyncio.subprocess 默认创建的子进程会分配新的控制台窗口——每次 shell 命令、
grep、后台任务启动都会闪一个 PowerShell 窗口。设置
CREATE_NO_WINDOW 让子进程静默运行（stdout/stderr 仍经管道回流）。
"""

from __future__ import annotations

import os

# CREATE_NO_WINDOW (0x08000000)：子进程不分配控制台窗口。
# 可与 CREATE_NEW_PROCESS_GROUP (0x00000200) 按位组合（bash_runner 用）。
CREATE_NO_WINDOW = 0x08000000


def no_window_creationflags(base: int = 0) -> int:
    """Windows 下返回 ``base | CREATE_NO_WINDOW``；非 Windows 原样返回 base。"""
    if os.name != "nt":
        return base
    return base | CREATE_NO_WINDOW
