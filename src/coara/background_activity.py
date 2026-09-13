"""后台级联工作探活：子智能体 / 后台 bash 任务 / 后台 agent 任一在跑即视为忙碌。

手机 typing 三态（typing... / typing 单点 / 灭）的判定依据；turn 结束信封
与 quiet 信封都以此为准。
"""

from __future__ import annotations


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
