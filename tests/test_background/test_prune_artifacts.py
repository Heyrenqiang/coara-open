"""_prune_terminal_task_artifacts：跨空间过滤 / watch 产物保留 / output_path 悬空清理"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.background.bash_runner import BashBackgroundRunner
from src.background.task_store import TaskRecord, TaskStatus, TaskStore


def _old_terminal_record(
    task_id: str,
    *,
    output_path: Path | None,
    workspace_dir: str,
    days_old: int = 30,
) -> TaskRecord:
    old = (datetime.now().astimezone() - timedelta(days=days_old)).isoformat()
    return TaskRecord(
        task_id=task_id,
        kind="bash",
        description="demo",
        status=TaskStatus.COMPLETED.value,
        created_at=old,
        updated_at=old,
        completed_at=old,
        command="echo hi",
        output_path=str(output_path) if output_path is not None else None,
        workspace_dir=workspace_dir,
    )


def _make_artifact(ws: Path, task_id: str) -> Path:
    task_dir = ws / ".coara" / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    out = task_dir / "output.log"
    out.write_text("log", encoding="utf-8")
    return out


def test_prune_skips_other_workspace_records(tmp_path: Path) -> None:
    """全局 home 共用 store：空间 A 的 prune 不得删空间 B 的产物目录"""
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    store = TaskStore(tmp_path / "home")
    out_a = _make_artifact(ws_a, "bash-aaaa0001")
    out_b = _make_artifact(ws_b, "bash-bbbb0002")
    store.save(_old_terminal_record("bash-aaaa0001", output_path=out_a, workspace_dir=str(ws_a)))
    store.save(_old_terminal_record("bash-bbbb0002", output_path=out_b, workspace_dir=str(ws_b)))

    removed = BashBackgroundRunner._prune_terminal_task_artifacts(store, ws_a)

    assert removed == 1
    assert not out_a.parent.exists()
    assert out_b.parent.exists(), "cross-workspace artifact must survive"
    rec_b = store.load("bash-bbbb0002")
    assert rec_b is not None and rec_b.output_path == str(out_b)


def test_prune_keeps_watch_task_artifacts(tmp_path: Path) -> None:
    """watch 任务的 output.log / watch_meta.json 是命中审计唯一证据，不清"""
    ws = tmp_path / "ws"
    store = TaskStore(tmp_path / "home")
    out = _make_artifact(ws, "bash-watch001")
    (out.parent / "watch_meta.json").write_text("{}", encoding="utf-8")
    store.save(_old_terminal_record("bash-watch001", output_path=out, workspace_dir=str(ws)))

    removed = BashBackgroundRunner._prune_terminal_task_artifacts(store, ws)

    assert removed == 0
    assert out.exists()
    assert (out.parent / "watch_meta.json").exists()


def test_prune_clears_output_path_after_removal(tmp_path: Path) -> None:
    """产物删除后清掉记录上的 output_path，消除指向已删路径的悬空"""
    ws = tmp_path / "ws"
    store = TaskStore(tmp_path / "home")
    out = _make_artifact(ws, "bash-dead0003")
    store.save(_old_terminal_record("bash-dead0003", output_path=out, workspace_dir=str(ws)))

    removed = BashBackgroundRunner._prune_terminal_task_artifacts(store, ws)

    assert removed == 1
    record = store.load("bash-dead0003")
    assert record is not None
    assert record.output_path is None


def test_prune_legacy_record_without_workspace_stamp_still_scoped_by_path(tmp_path: Path) -> None:
    """旧记录无 workspace_dir 打戳：仍由 output_path 前缀（task_root）守住边界"""
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    store = TaskStore(tmp_path / "home")
    out_b = _make_artifact(ws_b, "bash-bbbb0004")
    store.save(_old_terminal_record("bash-bbbb0004", output_path=out_b, workspace_dir=""))

    removed = BashBackgroundRunner._prune_terminal_task_artifacts(store, ws_a)

    assert removed == 0
    assert out_b.parent.exists()
    # 从归属空间视角 prune 时正常清理（路径前缀在 task_root 内）
    assert BashBackgroundRunner._prune_terminal_task_artifacts(store, ws_b) == 1
    assert not out_b.parent.exists()
