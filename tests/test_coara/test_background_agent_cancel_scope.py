"""回归：BackgroundAgentManager.cancel_all(workspace_dir=) 的路径比较要归一。

裸字符串比较（``launch == str(workspace_dir)``）会把同一目录的不同写法（大小写、
``.`` 段、分隔符混用）判成它空间任务而漏杀——一条 attach 端的 Ctrl+C 只该收窄
到本空间，漏杀就是「打断打不掉」。同文件的 ``_same_workspace_dir`` 已是正确口径。
"""

from __future__ import annotations

import os
from pathlib import Path

from src.coara.background_agent import BackgroundAgentManager, _same_workspace_dir


def test_same_workspace_dir_normalizes_equivalent_paths(tmp_path: Path) -> None:
    ws = tmp_path / "a"
    ws.mkdir()
    assert str(ws) + os.sep != str(ws)  # 两种写法确实不同（裸比较会漏杀）
    assert _same_workspace_dir(str(ws) + os.sep, str(ws)) is True


def test_cancel_all_matches_same_dir_written_differently(tmp_path: Path) -> None:
    manager = BackgroundAgentManager()
    manager._tasks.clear()
    manager._task_workspaces.clear()
    manager._cancel_requested.clear()

    ws = tmp_path / "a"
    ws.mkdir()
    manager._tasks["agent-x"] = None  # 占位（尚未起 asyncio.Task）
    manager._task_workspaces["agent-x"] = str(ws) + os.sep

    try:
        assert "agent-x" not in manager._cancel_requested
        assert manager.cancel_all(workspace_dir=str(ws)) == 1
        assert "agent-x" in manager._cancel_requested
    finally:
        manager._tasks.clear()
        manager._task_workspaces.clear()
        manager._cancel_requested.clear()
