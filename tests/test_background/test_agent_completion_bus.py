"""后台 agent 完成通知必须经接线总线发布（会话级 CoaraBase 没有 event_bus）。

回归：此前发布走 `parent_coara.event_bus`，会话 coara 无此属性，
`hasattr` 守卫静默跳过，后台子智能体完成结果永不注入主会话。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.coara.background_agent import BackgroundAgentManager
from src.core.events import TraceEvent


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


def _parent_coara(workspace_dir: str):
    """会话级 coara 桩：有 workspace_dir / identity / session_id，**没有** event_bus。"""
    return SimpleNamespace(
        workspace_dir=workspace_dir,
        identity=SimpleNamespace(coara_id="parent-coara"),
        session_id="sess-1",
        workspace_manager=None,
        _emit_trace=lambda *a, **k: None,
    )


@pytest.mark.asyncio
async def test_completion_publishes_via_wired_bus(tmp_path) -> None:
    manager = BackgroundAgentManager()
    bus = _FakeBus()
    manager.set_event_bus(bus)
    try:
        parent = _parent_coara(str(tmp_path))
        assert not hasattr(parent, "event_bus")  # 会话级 coara 的真实形状

        async def _job() -> str:
            return "done"

        task_id = await manager.start(
            parent,
            _job,
            task_id="sa-test-buswire",
            subagent_type="coaras",
            description="bus wiring regression",
        )
        task = manager._tasks.get(task_id)
        assert task is not None
        await task

        published = [e for e in bus.events if e.event_type == "background_task_complete"]
        assert len(published) == 1
        payload = published[0].payload or {}
        assert payload["task_id"] == "sa-test-buswire"
        assert payload["status"] == "completed"
        # 路由戳保留：Root 靠它们把完成通知投回发起会话
        assert payload["coara_id"] == "parent-coara"
        assert payload["session_id"] == "sess-1"
    finally:
        manager.set_event_bus(None)


@pytest.mark.asyncio
async def test_completion_skips_publish_without_any_bus(tmp_path) -> None:
    """总线未接线时不发布（也不许炸）——防守用例，固定静默降级行为。"""
    manager = BackgroundAgentManager()
    manager.set_event_bus(None)
    parent = _parent_coara(str(tmp_path))

    async def _job() -> str:
        return "done"

    task_id = await manager.start(parent, _job, task_id="sa-test-nobus", subagent_type="coaras", description="d")
    task = manager._tasks.get(task_id)
    assert task is not None
    await task  # 不抛异常即通过


@pytest.mark.asyncio
async def test_advisor_completion_publishes_but_janitor_not(tmp_path) -> None:
    """aide收官必须回投主会话（它就是给主会话找资料的）；janitor 系统派发不回投。

    回归：shadow 改名aide时把「结果静默」语义一并继承，aide被排除在
    收官注入之外，结果永不回主会话。
    """
    manager = BackgroundAgentManager()
    bus = _FakeBus()
    manager.set_event_bus(bus)
    try:
        parent = _parent_coara(str(tmp_path))

        async def _job() -> str:
            return "调研结果"

        advisor_id = await manager.start(parent, _job, task_id="sa-test-advisor", subagent_type="aide", description="d")
        advisor_task = manager._tasks[advisor_id]
        janitor_id = await manager.start(
            parent, _job, task_id="sa-test-janitor", subagent_type="janitor", description="d"
        )
        janitor_task = manager._tasks[janitor_id]
        await advisor_task
        await janitor_task

        published = [(e.payload or {}).get("task_id") for e in bus.events if e.event_type == "background_task_complete"]
        assert "sa-test-advisor" in published
        assert "sa-test-janitor" not in published
    finally:
        manager.set_event_bus(None)
