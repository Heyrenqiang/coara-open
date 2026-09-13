"""Background tasks: TaskStore, bash runner, output watch."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.background.bash_runner import BashBackgroundRunner
from src.background.output_watch import (
    OutputWatchConfig,
    OutputWatchMatcher,
    merge_output_watch_config,
    parse_output_watch_config,
)
from src.background.task_store import TaskRecord, TaskStatus, TaskStore
from src.background.task_store_paths import task_store_for_workspace
from src.tools.builtin.background.task_support import format_running_duration

# --- test_collapse_log_noise ---


def test_collapse_log_noise_folds_pytest_progress_wall() -> None:
    from src.utils.log_noise import collapse_log_noise

    lines = [f"{'.' * 72} [{pct:3d}%]" for pct in range(4, 100, 5)]
    text = "\n".join(["real output"] + lines + ["1518 passed in 97s"])
    out = collapse_log_noise(text)
    assert "（进度行 ×20 已折叠）" in out
    assert "real output" in out
    assert "1518 passed in 97s" in out
    assert "...." not in out.replace("（进度行 ×20 已折叠）", "")


def test_collapse_log_noise_folds_consecutive_duplicates() -> None:
    from src.utils.log_noise import collapse_log_noise

    out = collapse_log_noise("building\nbuilding\nbuilding\ndone")
    assert out == "building\n（上行 ×3）\ndone"


def test_collapse_log_noise_keeps_normal_text() -> None:
    from src.utils.log_noise import collapse_log_noise

    text = "line1\nline2\n\nline3"
    assert collapse_log_noise(text) == text


# --- test_task_store.py ---


@pytest.fixture
def store(tmp_path: Path):
    return TaskStore(base_dir=tmp_path)


@pytest.fixture
def bash_record():
    return TaskRecord(
        task_id="bash-test-1",
        kind="bash",
        description="Run tests",
        status=TaskStatus.RUNNING.value,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        command="pytest",
        output_path="/tmp/out.log",
    )


@pytest.fixture
def agent_record():
    return TaskRecord(
        task_id="sa-explore-abc",
        kind="agent",
        description="Explore codebase",
        status=TaskStatus.RUNNING.value,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        subagent_type="explore",
        agent_id="frac-1",
    )


class TestTaskStore:
    def test_workspace_store_returns_cached_instance(self, tmp_path: Path) -> None:
        from src.background.task_store_paths import reset_task_store_cache

        reset_task_store_cache()
        home = tmp_path / "home"
        first = task_store_for_workspace(tmp_path, configured_home=home)
        second = task_store_for_workspace(tmp_path, configured_home=home)
        assert first is second
        other = task_store_for_workspace(tmp_path, configured_home=tmp_path / "home2")
        assert other is not first

    def test_workspace_store_shared_instance_no_lost_update(self, tmp_path: Path) -> None:
        """同一 home 的两个写者共享实例与锁：update 交错不再后写覆盖先写。"""
        from src.background.task_store_paths import reset_task_store_cache

        reset_task_store_cache()
        home = tmp_path / "home"
        s1 = task_store_for_workspace(tmp_path, configured_home=home)
        s2 = task_store_for_workspace(tmp_path, configured_home=home)
        now = datetime.now().isoformat()
        s1.save(
            TaskRecord(
                task_id="t1",
                kind="bash",
                description="",
                status=TaskStatus.RUNNING.value,
                created_at=now,
                updated_at=now,
            )
        )
        assert s2.load("t1") is not None
        assert s1.update("t1", description="a")
        assert s2.update("t1", command="x")
        # 独立新实例从磁盘读回：两个字段都应在（旧实现后写覆盖丢 description）
        fresh = TaskStore(base_dir=home)
        loaded = fresh.load("t1")
        assert loaded is not None
        assert loaded.description == "a"
        assert loaded.command == "x"

    def test_save_and_load(self, store, bash_record):
        store.save(bash_record)
        loaded = store.load("bash-test-1")
        assert loaded is not None
        assert loaded.task_id == "bash-test-1"
        assert loaded.kind == "bash"
        assert loaded.status == TaskStatus.RUNNING.value
        assert loaded.command == "pytest"

    def test_load_missing_returns_none(self, store):
        assert store.load("nonexistent") is None

    def test_update_fields(self, store, bash_record):
        store.save(bash_record)
        updated = store.update("bash-test-1", status=TaskStatus.COMPLETED.value, exit_code=0)
        assert updated is True
        loaded = store.load("bash-test-1")
        assert loaded.status == TaskStatus.COMPLETED.value
        assert loaded.exit_code == 0

    def test_update_missing_returns_false(self, store):
        assert store.update("nonexistent", status=TaskStatus.FAILED.value) is False

    def test_delete(self, store, bash_record):
        store.save(bash_record)
        assert store.delete("bash-test-1") is True
        assert store.load("bash-test-1") is None
        assert store.delete("bash-test-1") is False

    def test_list_all_newest_first(self, store):
        base = datetime.now()
        for i in range(3):
            record = TaskRecord(
                task_id=f"task-{i}",
                kind="bash",
                description=f"task {i}",
                status=TaskStatus.COMPLETED.value,
                created_at=base.isoformat(),
                updated_at=base.isoformat(),
            )
            store.save(record)
            # Ensure distinct timestamps for deterministic ordering
            base = datetime.fromtimestamp(base.timestamp() + 1)
        records = store.list_all()
        assert len(records) == 3
        # Newest first
        assert records[0].task_id == "task-2"
        assert records[2].task_id == "task-0"

    def test_list_active_filters_status(self, store, bash_record, agent_record):
        store.save(bash_record)
        store.save(agent_record)

        active = store.list_active()
        assert len(active) == 2

        store.update("bash-test-1", status=TaskStatus.COMPLETED.value)
        active = store.list_active()
        assert len(active) == 1
        assert active[0].task_id == "sa-explore-abc"

    def test_prune_old_records(self, store):
        for i in range(505):
            record = TaskRecord(
                task_id=f"task-{i:03d}",
                kind="bash",
                description="x",
                status=TaskStatus.COMPLETED.value,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )
            store.save(record)
        assert len(store.list_all()) <= 500

    def test_agent_record_fields(self, store, agent_record):
        store.save(agent_record)
        loaded = store.load("sa-explore-abc")
        assert loaded.subagent_type == "explore"
        assert loaded.agent_id == "frac-1"
        assert loaded.command is None
        assert loaded.output_path is None


# --- test_bash_runner_store.py ---


@pytest.mark.asyncio
async def test_create_task_persists_to_coara_home(isolated_coara_home: Path, tmp_path: Path) -> None:
    runner = BashBackgroundRunner()
    runner._tasks.clear()
    runner._task_stores.clear()

    with (
        patch.object(runner, "_publish_completion"),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
    ):
        process = AsyncMock()
        process.pid = 12345
        process.stdout = None
        process.stderr = None
        process.wait = AsyncMock(return_value=0)
        process.returncode = 0
        spawn.return_value = process

        task_id = await runner.create_task(
            "echo hello",
            "test job",
            workspace_dir=tmp_path,
            coara_home=isolated_coara_home,
        )

    store = task_store_for_workspace(tmp_path, configured_home=isolated_coara_home)
    record = store.load(task_id)
    assert record is not None
    assert record.status == TaskStatus.RUNNING.value
    assert record.kind == "bash"

    pending = runner._tasks.get(task_id)
    if pending is not None and not pending.done():
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending


@pytest.mark.asyncio
async def test_create_task_argv_bypasses_shell_wrapper(tmp_path: Path) -> None:
    """Direct argv must not go through PowerShell EncodedCommand."""
    runner = BashBackgroundRunner()
    runner._tasks.clear()
    runner._task_stores.clear()

    with (
        patch.object(runner, "_publish_completion"),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
    ):
        process = AsyncMock()
        process.pid = 99
        process.stdout = None
        process.stderr = None
        process.wait = AsyncMock(return_value=0)
        process.returncode = 0
        spawn.return_value = process

        argv = ["python", "-c", "print(42)"]
        task_id = await runner.create_task(
            "display only",
            "direct argv",
            workspace_dir=tmp_path,
            argv=argv,
            timeout=5.0,
        )
        await asyncio.sleep(0.05)

    assert spawn.await_count >= 1
    called_args = spawn.await_args.args
    assert called_args[: len(argv)] == tuple(argv)
    assert not any("powershell" in str(a).lower() for a in called_args)

    pending = runner._tasks.get(task_id)
    if pending is not None and not pending.done():
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending


# --- format_running_duration ---


def test_format_running_duration_seconds() -> None:
    start = datetime(2026, 1, 1, 12, 0, 0)
    now = start + timedelta(seconds=42)
    text = format_running_duration(start.isoformat(), now=now)
    assert text == " (已运行 42s)"


def test_format_running_duration_minutes() -> None:
    start = datetime(2026, 1, 1, 12, 0, 0)
    now = start + timedelta(minutes=3, seconds=5)
    text = format_running_duration(start.isoformat(), now=now)
    assert text == " (已运行 3m 5s)"



# --- test_output_watch.py ---


def test_parse_output_watch_config_string_and_dict() -> None:
    cfg = parse_output_watch_config("ERROR")
    assert cfg is not None
    assert cfg.pattern == "ERROR"

    cfg2 = parse_output_watch_config({"pattern": "done", "debounce_ms": 100, "max_notifications": 2})
    assert cfg2 is not None
    assert cfg2.debounce_ms == 100
    assert cfg2.max_notifications == 2


def test_output_watch_matcher_respects_debounce_and_max(tmp_path, monkeypatch) -> None:
    class FakeMonotonic:
        def __init__(self) -> None:
            self.t = 0.0

        def advance(self, seconds: float) -> None:
            self.t += seconds

        def __call__(self) -> float:
            return self.t

    clock = FakeMonotonic()
    monkeypatch.setattr("src.background.output_watch.time.monotonic", clock)

    matches: list[dict] = []

    matcher = OutputWatchMatcher(
        config=OutputWatchConfig(pattern="READY", debounce_ms=50, max_notifications=2),
        task_id="bash-test",
        log_path=tmp_path / "output.log",
        on_match=matches.append,
    )
    matcher.feed_line("waiting")
    matcher.feed_line("READY now")
    clock.advance(0.02)
    matcher.feed_line("READY again")
    assert len(matches) == 1
    assert matches[0]["matched_line"] == "READY now"

    clock.advance(0.06)
    matcher.feed_line("READY third")
    assert len(matches) == 2

    clock.advance(0.06)
    matcher.feed_line("READY fourth")
    assert len(matches) == 2


def test_merge_output_watch_config_applies_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.background.output_watch.get_shell_notify_config_defaults",
        lambda: OutputWatchConfig(pattern="", debounce_ms=9000, max_notifications=5),
    )
    merged = merge_output_watch_config("ERROR")
    assert merged is not None
    assert merged.pattern == "ERROR"
    assert merged.debounce_ms == 9000
    assert merged.max_notifications == 5



# --- test_background_default_timeout.py ---


def test_resolve_background_task_timeout_default_and_override(monkeypatch) -> None:
    from src.background.bash_runner import resolve_background_task_timeout
    from src.core.config import config_manager

    monkeypatch.setattr(config_manager, "_raw_config", {}, raising=False)
    assert resolve_background_task_timeout() == 7200.0

    monkeypatch.setattr(config_manager, "_raw_config", {"background_task_timeout_seconds": 300}, raising=False)
    assert resolve_background_task_timeout() == 300.0

    monkeypatch.setattr(config_manager, "_raw_config", {"background_task_timeout_seconds": 0}, raising=False)
    assert resolve_background_task_timeout() is None


async def _run_hanging_task(
    runner: BashBackgroundRunner,
    tmp_path: Path,
    home: Path,
    **kwargs,
) -> str:
    """Start a task whose subprocess hangs forever; terminate mock marks it dead."""

    async def _hang() -> int:
        await asyncio.sleep(60)
        return 0

    with (
        patch.object(runner, "_publish_completion"),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        patch("src.background.bash_runner.terminate_subprocess_tree", new_callable=AsyncMock) as term,
    ):
        process = AsyncMock()
        process.pid = 5150
        process.stdout = None
        process.stderr = None
        process.wait = _hang
        process.returncode = None

        async def _kill(p, **_kwargs) -> None:
            p.returncode = -9

        term.side_effect = _kill
        spawn.return_value = process

        task_id = await runner.create_task(
            "sleep 60",
            "挂起的任务",
            workspace_dir=tmp_path,
            coara_home=home,
            **kwargs,
        )
        task = runner._tasks.get(task_id)
        assert task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return task_id


@pytest.fixture
def clean_runner() -> BashBackgroundRunner:
    runner = BashBackgroundRunner()
    runner._tasks.clear()
    runner._task_stores.clear()
    runner._task_sessions.clear()
    return runner


@pytest.mark.asyncio
async def test_background_default_timeout_marks_timed_out(
    clean_runner, isolated_coara_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """未显式传 timeout 时套用默认上限 挂死进程收敛为 timed_out 而非永远 running。"""
    monkeypatch.setattr("src.background.bash_runner.resolve_background_task_timeout", lambda: 0.05)

    task_id = await _run_hanging_task(clean_runner, tmp_path, isolated_coara_home)

    record = task_store_for_workspace(tmp_path, configured_home=isolated_coara_home).load(task_id)
    assert record is not None
    assert record.status == TaskStatus.TIMED_OUT.value
    assert record.timed_out is True


@pytest.mark.asyncio
async def test_background_explicit_timeout_not_overridden(
    clean_runner, isolated_coara_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """默认兜底关闭时 显式传入的 timeout 仍然生效。"""
    monkeypatch.setattr("src.background.bash_runner.resolve_background_task_timeout", lambda: None)

    task_id = await _run_hanging_task(clean_runner, tmp_path, isolated_coara_home, timeout=0.05)

    record = task_store_for_workspace(tmp_path, configured_home=isolated_coara_home).load(task_id)
    assert record is not None
    assert record.status == TaskStatus.TIMED_OUT.value
    assert record.timed_out is True


@pytest.mark.asyncio
async def test_background_output_watch_task_skips_default_timeout(
    clean_runner, isolated_coara_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """输出监控任务不套默认上限：短兜底不生效 任务保持 running 直至手动停止。"""
    monkeypatch.setattr("src.background.bash_runner.resolve_background_task_timeout", lambda: 0.05)
    monkeypatch.setattr("src.background.output_watch.is_shell_notify_enabled", lambda: True)

    store = task_store_for_workspace(tmp_path, configured_home=isolated_coara_home)

    async def _hang() -> int:
        await asyncio.sleep(60)
        return 0

    with (
        patch.object(clean_runner, "_publish_completion"),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        patch("src.background.bash_runner.terminate_subprocess_tree", new_callable=AsyncMock) as term,
    ):
        process = AsyncMock()
        process.pid = 5151
        process.stdout = None
        process.stderr = None
        process.wait = _hang
        process.returncode = None

        async def _kill(p, **_kwargs) -> None:
            p.returncode = -9

        term.side_effect = _kill
        spawn.return_value = process

        task_id = await clean_runner.create_task(
            "sleep 60",
            "监控任务",
            workspace_dir=tmp_path,
            coara_home=isolated_coara_home,
            output_watch={"pattern": "READY", "reason": "测试"},
        )
        # 超过兜底的 0.05s 后任务仍在跑
        await asyncio.sleep(0.2)
        record = store.load(task_id)
        assert record is not None
        assert record.status == TaskStatus.RUNNING.value

        await clean_runner.stop_task(task_id)
        task = clean_runner._tasks.get(task_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # 换一个新实例从磁盘重读 避开本实例缓存
    record = task_store_for_workspace(tmp_path, configured_home=isolated_coara_home).load(task_id)
    assert record is not None
    assert record.status == TaskStatus.KILLED.value
