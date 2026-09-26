"""后台级联工作探活：子智能体 / 后台 bash 任务 / 后台 agent 任一在跑即视为忙碌"""

from __future__ import annotations

from typing import Any


def background_work_active() -> bool:
    """True while any cascaded background work is still running."""
    from src.background.bash_runner import BashBackgroundRunner
    from src.coara.background_agent import BackgroundAgentManager
    from src.tools.builtin.delegate.delegate import _RUNNING_SUBAGENTS

    if _RUNNING_SUBAGENTS:
        return True
    if any(t is not None and not t.done() for t in BashBackgroundRunner()._tasks.values()):
        return True
    return any(t is not None and not t.done() for t in BackgroundAgentManager()._tasks.values())


def active_delegation_rows(workspace_dir: str = "") -> list[dict[str, Any]]:
    """在跑子智能体的展示行（端侧活动树重建用）"""
    from src.tools.builtin.delegate.delegate import _RUNNING_SUBAGENT_META

    wanted = str(workspace_dir or "")
    rows: list[dict[str, Any]] = []
    for meta in list(_RUNNING_SUBAGENT_META.values()):
        if wanted and str(meta.get("workspace_dir") or "") != wanted:
            continue
        rows.append(dict(meta))
    rows.sort(key=lambda m: float(m.get("started_at") or 0.0))
    return rows
