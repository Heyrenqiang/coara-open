"""Safe asyncio subprocess termination (foreground shell + background bash)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from src.core.logger import logger
from src.utils.win_proc import no_window_creationflags


async def _taskkill_tree(pid: int, *, force: bool) -> None:
    """Windows: kill the process tree via taskkill (/T). 不带 /F 为温和终止，
    给进程收尾机会；带 /F 为强杀。"""
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    killer = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=no_window_creationflags(),
    )
    await killer.wait()


async def terminate_subprocess_tree(
    process: asyncio.subprocess.Process | None,
    *,
    grace_seconds: float = 2.0,
    graceful: bool = False,
) -> None:
    """Kill a subprocess and its children, close pipes, and reap the process.

    **超时路径直接强杀**（graceful=False，默认，任务已失败不留额外执行
    时间）；**取消路径先优雅**（graceful=True：SIGTERM / taskkill 不带
    /F → grace 窗口 → 升级强杀 SIGKILL / taskkill /F），给进程收尾机会
    防脏数据。两种路径都确保进程树（含孙进程）被杀净。

    Intended for timeout / cancel paths so the parent asyncio loop stays healthy.
    """
    if process is None or process.returncode is not None:
        return

    if os.name == "nt":
        try:
            if graceful:
                await _taskkill_tree(process.pid, force=False)
                try:
                    await asyncio.wait_for(process.wait(), timeout=grace_seconds)
                except TimeoutError:
                    await _taskkill_tree(process.pid, force=True)
            else:
                await _taskkill_tree(process.pid, force=True)
        except Exception as exc:
            logger.debug(f"taskkill tree failed for pid {process.pid}: {exc}")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    else:
        try:
            import signal

            # 以下 os.getpgid / os.killpg 是 POSIX 专有 API：Windows 走上面的
            # taskkill 分支，本块永不执行。mypy 在 Windows 上从 os 存根读不到
            # 这些符号，故逐点标注（属平台差异，不是真实缺陷）。
            pgid = os.getpgid(process.pid)  # type: ignore[attr-defined]
            # 防御：子进程默认不建新会话时 pgid 取到的是父组（即 coara 自己），
            # killpg 会把 coara 进程一并击杀——pgid 与自身组相同时只杀子进程
            own_pgid = os.getpgid(0)  # type: ignore[attr-defined]
            own_pid = os.getpid()
            if pgid in (own_pgid, own_pid):
                logger.warning(
                    f"subprocess pid {process.pid} shares coara's process group ({pgid}); "
                    "killing only the child, not the group"
                )
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            elif graceful:
                os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined]
                try:
                    await asyncio.wait_for(process.wait(), timeout=grace_seconds)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined]
            else:
                os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined]
        except (ProcessLookupError, OSError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)

    # Close the stdout/stderr pipe transports so communicate() below returns
    # promptly even if a surviving descendant still holds the pipe handles
    # open (otherwise it blocks until the grace timeout).
    transport = getattr(process, "_transport", None)
    pipes = getattr(transport, "_pipes", None) or {}
    for pipe_transport in list(pipes.values()):
        with contextlib.suppress(Exception):
            pipe_transport.close()

    # Drain/closing pipes releases asyncio transports.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.communicate(), timeout=grace_seconds)


async def await_with_timeout(awaitable: Any, *, timeout: float, label: str) -> Any:
    """Await ``awaitable`` with a cap; return None on timeout (log at debug)."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError:
        logger.debug(f"{label} did not finish within {timeout}s")
        return None
