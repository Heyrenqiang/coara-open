"""CLI background task label formatting."""

from __future__ import annotations

from src.background.task_store import TaskKind, TaskRecord, TaskStatus
from src.cli.background_tasks_display import BackgroundTasksSnapshot, format_task_label


def _rec(*, kind: str, description: str, subagent_type: str | None = None) -> TaskRecord:
    return TaskRecord(
        task_id="t1",
        kind=kind,
        description=description,
        status=TaskStatus.RUNNING.value,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        subagent_type=subagent_type,
    )


def test_format_task_label_bash_and_agent() -> None:
    assert format_task_label(_rec(kind=TaskKind.BASH.value, description="pytest")).startswith("bash:")
    assert format_task_label(_rec(kind=TaskKind.AGENT.value, description="探查", subagent_type="explore")).startswith(
        "explore:"
    )


def test_idle_prompt_line_no_gear() -> None:
    snap = BackgroundTasksSnapshot(
        (
            _rec(
                kind=TaskKind.AGENT.value,
                description="探查",
                subagent_type="explore",
            ),
        )
    )
    line = snap.idle_prompt_line()
    assert line == "后台 explore: 探查"
    assert "⚙" not in line
