"""CLI background task label formatting."""

from __future__ import annotations

from src.background.task_store import TaskKind, TaskRecord, TaskStatus
from src.cli.background_tasks_display import BackgroundTasksSnapshot, format_task_label, snapshot_running_tasks


def _rec(
    *,
    kind: str,
    description: str,
    subagent_type: str | None = None,
    origin_source: str = "",
    workspace_dir: str = "",
) -> TaskRecord:
    return TaskRecord(
        task_id="t1",
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
