"""CLI background task label formatting."""

from __future__ import annotations

import pytest

from src.background.task_store import TaskKind, TaskRecord, TaskStatus
from src.cli.background_tasks_display import BackgroundTasksSnapshot, format_task_label, snapshot_running_tasks


def _rec(
    *,
    kind: str,
    description: str,
    subagent_type: str | None = None,
    origin_source: str = "",
    workspace_dir: str = "",
    task_id: str = "t1",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        kind=kind,
        description=description,
        status=TaskStatus.RUNNING.value,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        subagent_type=subagent_type,
        origin_source=origin_source,
        workspace_dir=workspace_dir,
    )


def test_format_task_label_bash_and_agent() -> None:
    assert format_task_label(_rec(kind=TaskKind.BASH.value, description="pytest")).startswith("bash:")
    assert format_task_label(_rec(kind=TaskKind.AGENT.value, description="探查", subagent_type="explore")).startswith(
        "explore:"
    )


def test_labels_for_toolbar_rotation() -> None:
    snap = BackgroundTasksSnapshot(
        (
            _rec(kind=TaskKind.AGENT.value, description="探查", subagent_type="explore"),
            _rec(kind=TaskKind.BASH.value, description="pytest"),
        )
    )
    assert snap.labels() == ("explore: 探查", "bash: pytest")
    assert snap.count == 2
    summary = snap.summary()
    assert summary == {"count": 2, "labels": ["explore: 探查", "bash: pytest"]}


def test_snapshot_origin_end_keeps_same_family_only(monkeypatch) -> None:
    class _Store:
        def list_active(self):
            return [
                _rec(kind=TaskKind.BASH.value, description="cli-job", origin_source="cli-attached"),
                _rec(kind=TaskKind.BASH.value, description="web-job", origin_source="web"),
                _rec(kind=TaskKind.BASH.value, description="orphan", origin_source=""),
            ]

    class _Root:
        workspace_dir = "D:/ws"

    store = _Store()
    monkeypatch.setattr(
        "src.background.task_store_paths.task_store_for_coara",
        lambda _r: store,
    )
    snap = snapshot_running_tasks(_Root(), origin_end="cli-attached")
    assert snap.count == 1
    assert snap.labels() == ("bash: cli-job",)
    snap_web = snapshot_running_tasks(_Root(), origin_end="web")
    assert snap_web.count == 1
    assert snap_web.labels() == ("bash: web-job",)


def test_require_live_drops_and_heals_zombie_running(monkeypatch) -> None:
    """TaskStore RUNNING but runner gone → exclude from spinner and mark FAILED."""
    zombie = _rec(kind=TaskKind.AGENT.value, description="卡住", subagent_type="aide", origin_source="web")
    live = _rec(kind=TaskKind.BASH.value, description="pytest", origin_source="web", task_id="live-1")
    updates: list[tuple[str, dict]] = []

    class _Store:
        def list_active(self):
            return [zombie, live]

        def update(self, task_id: str, **fields):
            updates.append((task_id, fields))
            return True

    class _Root:
        workspace_dir = "D:/ws"

    monkeypatch.setattr(
        "src.background.task_store_paths.task_store_for_coara",
        lambda _r: _Store(),
    )
    monkeypatch.setattr(
        "src.cli.background_tasks_display._manager_tracks_live",
        lambda task_id, *, kind: task_id == "live-1",
    )
    snap = snapshot_running_tasks(_Root(), origin_end="web", require_live=True)
    assert snap.count == 1
    assert snap.labels() == ("bash: pytest",)
    assert updates and updates[0][0] == "t1"
    assert updates[0][1]["status"] == TaskStatus.FAILED.value


def test_snapshot_items_include_task_id() -> None:
    snap = BackgroundTasksSnapshot(
        (
            _rec(kind=TaskKind.AGENT.value, description="探查", subagent_type="explore", task_id="sa-1"),
            _rec(kind=TaskKind.BASH.value, description="pytest", task_id="bash-1"),
        )
    )
    assert snap.items() == (
        {
            "task_id": "sa-1",
            "kind": TaskKind.AGENT.value,
            "label": "explore: 探查",
            "description": "探查",
            "subagent_type": "explore",
            "origin_source": "",
        },
        {
            "task_id": "bash-1",
            "kind": TaskKind.BASH.value,
            "label": "bash: pytest",
            "description": "pytest",
            "subagent_type": "",
            "origin_source": "",
        },
    )


@pytest.mark.asyncio
async def test_kill_background_task_respects_origin_and_marks_killed(monkeypatch) -> None:
    from src.cli.background_tasks_display import kill_background_task

    record = _rec(
        kind=TaskKind.AGENT.value,
        description="卡住",
        subagent_type="aide",
        origin_source="web",
        task_id="bg-1",
        workspace_dir="D:/ws",
    )
    updates: list[tuple[str, dict]] = []

    class _Store:
        def load(self, task_id: str):
            return record if task_id == "bg-1" else None

        def update(self, task_id: str, **fields):
            updates.append((task_id, fields))
            record.status = fields.get("status", record.status)
            return True

    class _Root:
        workspace_dir = "D:/ws"

    class _Mgr:
        async def cancel(self, task_id: str) -> bool:
            assert task_id == "bg-1"
            return True

    monkeypatch.setattr("src.background.task_store_paths.task_store_for_coara", lambda _r: _Store())
    monkeypatch.setattr("src.coara.background_agent.BackgroundAgentManager", lambda: _Mgr())

    denied = await kill_background_task(_Root(), "bg-1", origin_end="cli-attached")
    assert denied == {"ok": False, "killed": False, "reason": "forbidden"}

    result = await kill_background_task(_Root(), "bg-1", origin_end="web")
    assert result["ok"] is True
    assert result["killed"] is True
    assert result["task_id"] == "bg-1"
    assert updates and updates[0][0] == "bg-1"
    assert updates[0][1]["status"] == TaskStatus.KILLED.value
