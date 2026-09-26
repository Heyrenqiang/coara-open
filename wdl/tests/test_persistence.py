"""persistence 建/读/终态/孤儿恢复。必须 await close：aiosqlite 连接线程
非守护，不关进程不退（coara 侧踩过的坑）。"""

from __future__ import annotations

import pytest

from wdl.persistence import WorkflowPersistence
from wdl.types import WorkflowInstanceState


@pytest.mark.asyncio
async def test_instance_lifecycle(tmp_path) -> None:
    p = WorkflowPersistence(db_path=tmp_path / "instances.db", owner_id="test-owner")
    try:
        await p.create_instance("i1", name="demo", wdl_text="name: demo\nnodes:\n  a:\n    task: x\n", inputs={"k": 1})
        await p.save_state("i1", None, {}, status=WorkflowInstanceState.RUNNING)
        row = await p.load_instance("i1")
        assert row is not None
        assert row.status == WorkflowInstanceState.RUNNING

        ok = await p.complete_instance("i1", {"a": {"text": "done"}})
        assert ok is True
        row = await p.load_instance("i1")
        assert row is not None
        assert row.status == WorkflowInstanceState.COMPLETED
        assert row.context_snapshot["a"]["text"] == "done"

        # 终态守卫：已完成的实例不可再取消
        assert await p.cancel_instance("i1") is False
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_recover_orphaned_running(tmp_path) -> None:
    db = tmp_path / "instances.db"
    p = WorkflowPersistence(db_path=db, owner_id="engine-old")
    await p.create_instance("i2", name="demo", wdl_text="name: demo\nnodes:\n  a:\n    task: x\n", inputs={})
    await p.save_state("i2", None, {}, status=WorkflowInstanceState.RUNNING)
    await p.close()

    # 新进程（无 owner）启动：running → interrupted
    p2 = WorkflowPersistence(db_path=db)
    try:
        assert await p2.recover_orphaned_running() == 1
        row = await p2.load_instance("i2")
        assert row is not None
        assert row.status == WorkflowInstanceState.INTERRUPTED
    finally:
        await p2.close()
