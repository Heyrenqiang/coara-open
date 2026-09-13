"""Shell tool: foreground timeout is a failure (codex-style).

前台超时即失败：executor 的 wait_for 到点取消任务 → shell 的 CancelledError
路径杀进程树 → 返回明确超时错误；显式后台（timeout_ms=0）仍走
BashBackgroundRunner 任务。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.core.types import ToolCall
from src.tools.builtin.runtime.shell import ShellTool
from tests.helpers import make_test_coara


@pytest.mark.asyncio
async def test_shell_timeout_fails_and_kills_process(tmp_path: Path) -> None:
    """前台超时即失败：进程被终止、标记文件未写、返回明确超时错误。"""
    marker = tmp_path / "marker.txt"
    cmd = f'Start-Sleep -Milliseconds 1500; Set-Content -LiteralPath "{marker}" -Value done'
    coara = make_test_coara(tmp_path)
    coara.register_tool(ShellTool())
    await coara.initialize()

    executor = ToolExecutor(default_timeout=120.0)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-s", name="shell", arguments={"command": cmd, "timeout_ms": 500})],
        is_owner=True,
    )

    result = executions[0].result
    assert result.is_error
    assert "timed out" in (result.content or "").lower()
    # 命令在写 marker 之前被杀：进程树终止、后续命令未执行
    assert not marker.exists(), "标记文件被写入：进程未被终止"


@pytest.mark.asyncio
async def test_shell_fast_command_untouched(tmp_path: Path) -> None:
    """快命令不受影响：正常返回真实输出，不误报超时。"""
    coara = make_test_coara(tmp_path)
    coara.register_tool(ShellTool())
    await coara.initialize()

    executor = ToolExecutor(default_timeout=120.0)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-f", name="shell", arguments={"command": "Write-Output fast-ok"})],
        is_owner=True,
    )
    result = executions[0].result
    assert not result.is_error
    assert "fast-ok" in (result.content or "")


@pytest.mark.asyncio
async def test_shell_explicit_background_still_bg_task(tmp_path: Path) -> None:
    """显式后台（timeout_ms=0）路径不变：仍是 BashBackgroundRunner 任务。"""
    from src.background.bash_runner import BashBackgroundRunner
    from src.background.task_store import TaskStatus

    cmd = "Write-Output bg-ok"
    coara = make_test_coara(tmp_path)
    coara.register_tool(ShellTool())
    await coara.initialize()

    executor = ToolExecutor(default_timeout=120.0)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-b", name="shell", arguments={"command": cmd, "timeout_ms": 0})],
        is_owner=True,
    )
    result = executions[0].result
    assert not result.is_error
    assert result.metadata.get("background") is True
    task_id = result.metadata["task_id"]

    runner = BashBackgroundRunner()
    task = runner._tasks.get(task_id)
    store = runner._task_stores.get(task_id)
    assert task is not None and store is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=10)
    record = store.load(task_id)
    assert record is not None
    assert record.status == TaskStatus.COMPLETED.value
