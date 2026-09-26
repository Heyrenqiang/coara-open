"""后台子智能体任务台账：启动落盘、终态更新、重启中断提示。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.coara.background_agent import BackgroundAgentManager
from src.coara.subagent_task_ledger import (
    record_started,
    record_terminal,
    report_interrupted_tasks,
    running_records,
)
from src.workspace.updates.store import WorkspaceUpdatesStore
from tests.helpers import make_test_coara


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    BackgroundAgentManager._instance = None
    yield
    BackgroundAgentManager._instance = None


def _workspace_name_for(workspace_dir: Path) -> str:
    from src.core.coara_home import CoaraHomePaths

    return CoaraHomePaths.for_workspace(workspace_dir, None, migrate=False).workspace_id


def _workspace_name_via_registry(workspace_dir: Path) -> str:
    """与真实 Root 启动一致：registry 登记空间后以其 name 落动态。"""
    from src.core.coara_home import resolve_coara_home
    from src.workspace.registry import WorkspaceRegistry

    registry = WorkspaceRegistry(resolve_coara_home(workspace_dir, None))
    registry.load()
    entry = registry.ensure_workspace(workspace_dir)
    return str(entry.name)


@pytest.mark.asyncio
async def test_subagent_interrupted_start_persists_record(tmp_path: Path) -> None:
    """启动后台子智能体 → 台账落一行 running 记录。"""
    parent = make_test_coara(tmp_path)
    started = asyncio.Event()

    async def _run() -> str:
        started.set()
        await asyncio.sleep(3600)
        return "never"

    task_id = await BackgroundAgentManager().start(
        parent,
        _run,
        task_id="sa-test-1",
        subagent_type="coaras",
        description="摸排后台任务体系",
    )
    assert task_id == "sa-test-1"

    records = running_records(tmp_path, None)
    assert len(records) == 1
    record = records[0]
    assert record["task_id"] == "sa-test-1"
    assert record["subagent_type"] == "coaras"
    assert record["description"] == "摸排后台任务体系"
    assert record["workspace_dir"] == str(tmp_path)
    assert record["session_id"] == parent.session_id
    assert record["started_at"]
    assert record["status"] == "running"

    await BackgroundAgentManager().cancel(task_id)
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_subagent_interrupted_completion_updates_status(tmp_path: Path) -> None:
    """正常完成 → 追加 completed 终态，折叠后不再有 running 记录。"""
    parent = make_test_coara(tmp_path)

    async def _run() -> str:
        return "done"

    await BackgroundAgentManager().start(
        parent,
        _run,
        task_id="sa-test-2",
        subagent_type="aide",
        description="查个资料",
    )
    await asyncio.sleep(0.1)

    assert running_records(tmp_path, None) == []

    ledger = tmp_path / "coara-home" / "workspaces" / _workspace_name_for(tmp_path) / "subagent_tasks.jsonl"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    statuses = [(line["task_id"], line["status"]) for line in lines]
    assert ("sa-test-2", "running") in statuses
    assert ("sa-test-2", "completed") in statuses


@pytest.mark.asyncio
async def test_subagent_interrupted_failure_updates_status(tmp_path: Path) -> None:
    """协程抛异常 → 追加 failed 终态。"""
    parent = make_test_coara(tmp_path)

    async def _run() -> str:
        raise RuntimeError("boom")

    await BackgroundAgentManager().start(
        parent,
        _run,
        task_id="sa-test-3",
        subagent_type="coaras",
        description="会失败的任务",
    )
    await asyncio.sleep(0.1)

    assert running_records(tmp_path, None) == []


def test_subagent_interrupted_restart_writes_updates(tmp_path: Path) -> None:
    """重启扫描：running 记录写入工作空间动态，并落 interrupted 终态（幂等不重复提示）。"""
    record_started(
        tmp_path,
        None,
        task_id="sa-dead-1",
        subagent_type="coaras",
        description="死前在跑的活",
        session_id="sess-1",
    )

    root = SimpleNamespace(
        workspace_manager=SimpleNamespace(
            coara_home=tmp_path / "coara-home",
            name_for_path=lambda path: _workspace_name_via_registry(Path(path)),
        ),
        _updates_store=lambda: WorkspaceUpdatesStore(tmp_path / "coara-home"),
    )

    reported = report_interrupted_tasks(root, tmp_path, None)
    assert reported == 1

    store: Any = WorkspaceUpdatesStore(tmp_path / "coara-home")
    messages = store.list_messages(workspace=_workspace_name_via_registry(tmp_path), status="all")
    assert len(messages) == 1
    message = messages[0]
    assert message.event_type == "subagent_interrupted"
    assert message.salience == "high"
    assert "sa-dead-1" in message.text
    assert "coaras" in message.text
    assert "死前在跑的活" in message.text
    assert "进程意外中断" in message.text

    # 终态已落 interrupted：再次重启扫描不再重复提示
    assert running_records(tmp_path, None) == []
    assert report_interrupted_tasks(root, tmp_path, None) == 0


def test_subagent_interrupted_restart_skips_terminal_records(tmp_path: Path) -> None:
    """已收官（completed/failed）的记录不会被当作中断提示。"""
    record_started(
        tmp_path,
        None,
        task_id="sa-done-1",
        subagent_type="aide",
        description="跑完的活",
        session_id="sess-2",
    )
    record_terminal(tmp_path, None, task_id="sa-done-1", status="completed")

    monkeypatch_root = SimpleNamespace(workspace_manager=None, _updates_store=lambda: None)
    assert report_interrupted_tasks(monkeypatch_root, tmp_path, None) == 0

    # 台账未再追加行（没有新的 interrupted 终态）
    ledger = tmp_path / "coara-home" / "workspaces" / _workspace_name_for(tmp_path) / "subagent_tasks.jsonl"
    lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
