"""Startup recovery of stale TaskStore records and orphan bash processes.

A killed runner leaves detached bash subprocesses (CREATE_NEW_PROCESS_GROUP)
running while their records say ``running`` forever. Recovery marks them
failed, probes the recorded pid, and best-effort kills the orphan tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.background.task_store import TaskRecord, TaskStatus, TaskStore, _reap_orphan_process
from src.core.time import now_iso
from src.utils.win_proc import no_window_creationflags


def _record(task_id: str, *, status: str, pid: int | None = None, kind: str = "bash") -> TaskRecord:
    now = now_iso()
    return TaskRecord(
        task_id=task_id,
        kind=kind,
        description="demo",
        status=status,
        created_at=now,
        updated_at=now,
        command="sleep 60" if kind == "bash" else None,
        pid=pid,
    )


def test_recover_scans_multiple_workspace_dirs(tmp_path: Path) -> None:
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    TaskStore(ws_a).save(_record("bash-aaa", status=TaskStatus.RUNNING.value))
    TaskStore(ws_b).save(_record("bash-bbb", status=TaskStatus.RUNNING.value))
    TaskStore(ws_b).save(_record("bash-done", status=TaskStatus.COMPLETED.value))

    recovered = TaskStore.recover_stale_running(ws_a, ws_b)
    assert recovered == 2

    rec_a = TaskStore(ws_a).load("bash-aaa")
    rec_b = TaskStore(ws_b).load("bash-bbb")
    done = TaskStore(ws_b).load("bash-done")
    assert rec_a is not None and rec_a.status == TaskStatus.FAILED.value
    assert rec_b is not None and rec_b.status == TaskStatus.FAILED.value
    assert rec_b is not None and "restarted" in (rec_b.error or "")
    assert done is not None and done.status == TaskStatus.COMPLETED.value


def test_recover_writes_restart_notice(tmp_path: Path) -> None:
    """被回收的任务按空间写重启清算通知，供下次会话恢复时注入；通知一次性。"""
    from src.coara.workspace_state import consume_restart_notice

    ws = tmp_path / "ws"
    TaskStore(ws).save(_record("bash-aaa", status=TaskStatus.RUNNING.value))
    TaskStore(ws).save(_record("bash-done", status=TaskStatus.COMPLETED.value))

    assert TaskStore.recover_stale_running(ws) == 1

    notice = consume_restart_notice(ws)
    assert notice is not None
    assert notice.get("restart_at")
    items = [str(it) for it in notice["items"]]
    assert any("bash-aaa" in it and "demo" in it for it in items)
    assert not any("bash-done" in it for it in items)
    assert consume_restart_notice(ws) is None


def test_recover_dedupes_same_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    TaskStore(ws).save(_record("bash-dup", status=TaskStatus.RUNNING.value))
    recovered = TaskStore.recover_stale_running(ws, ws)
    assert recovered == 1


def test_recover_dead_pid_marks_failed_without_orphan_flag(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"], creationflags=no_window_creationflags())
    proc.wait(timeout=15)
    ws = tmp_path / "ws"
    TaskStore(ws).save(_record("bash-dead", status=TaskStatus.RUNNING.value, pid=proc.pid))

    recovered = TaskStore.recover_stale_running(ws)
    assert recovered == 1
    record = TaskStore(ws).load("bash-dead")
    assert record is not None
    assert record.status == TaskStatus.FAILED.value
    assert record.orphan_process_alive is False


def test_recover_kills_live_orphan_process(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], creationflags=no_window_creationflags()
    )
    try:
        ws = tmp_path / "ws"
        TaskStore(ws).save(_record("bash-live", status=TaskStatus.RUNNING.value, pid=proc.pid))

        recovered = TaskStore.recover_stale_running(ws)
        assert recovered == 1

        proc.wait(timeout=15)
        assert proc.poll() is not None
        record = TaskStore(ws).load("bash-live")
        assert record is not None
        assert record.status == TaskStatus.FAILED.value
        assert record.orphan_process_alive is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_reap_skips_non_bash_records(tmp_path: Path) -> None:
    """agent 类记录不做进程探测（也不会误杀当前测试进程）"""
    import os

    ws = tmp_path / "ws"
    store = TaskStore(ws)
    store.save(_record("agent-1", status=TaskStatus.FAILED.value, kind="agent", pid=os.getpid()))
    _reap_orphan_process(store, store.load("agent-1"))  # type: ignore[arg-type]
    record = store.load("agent-1")
    assert record is not None and record.orphan_process_alive is False


def test_reap_kill_failure_only_warns(tmp_path: Path, monkeypatch) -> None:
    """杀进程失败绝不抛出：启动恢复不能因此中断"""
    ws = tmp_path / "ws"
    store = TaskStore(ws)
    store.save(_record("bash-stubborn", status=TaskStatus.FAILED.value, pid=12345))
    monkeypatch.setattr("src.background.task_store._orphan_pid_alive", lambda pid, created_at: True)

    def _boom(pid: int) -> None:
        raise RuntimeError("cannot kill")

    monkeypatch.setattr("src.background.task_store._kill_process_tree", _boom)

    record = store.load("bash-stubborn")
    assert record is not None
    _reap_orphan_process(store, record)  # 不抛出
    updated = store.load("bash-stubborn")
    assert updated is not None and updated.orphan_process_alive is True
