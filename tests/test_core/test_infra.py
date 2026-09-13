"""Core infrastructure: persistence, schedulers, file tail."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.core.coara_home import (
    ensure_user_layout,
    ensure_workspace_layout,
    purge_stale_matters_card_files,
    resolve_bootstrap_coara_home,
    user_paths,
)
from src.core.config import config_manager
from src.core.file_tail import read_tail_text
from src.core.json_store import write_bytes_atomic, write_json_atomic, write_text_atomic
from src.core.tick_scheduler import TickSchedulerBase
from src.reminders.store import ReminderStore
from src.reminders.types import ReminderRecord


def test_write_json_atomic_uses_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "store.json"
    write_json_atomic(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_text_atomic_uses_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "runs.jsonl"
    write_text_atomic(target, '{"id":"a"}\n')
    assert target.read_text(encoding="utf-8") == '{"id":"a"}\n'


@pytest.mark.parametrize("writer", ["text", "json", "bytes"])
def test_atomic_writes_fsync_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    """断电安全：replace 前必须对临时文件 flush+fsync"""
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def spy_replace(src: object, dst: object) -> None:
        events.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    target = tmp_path / "store.json"
    if writer == "text":
        write_text_atomic(target, "hello")
    elif writer == "json":
        write_json_atomic(target, {"ok": True})
    else:
        write_bytes_atomic(target, b"\x00\x01")

    assert events[:2] == ["fsync", "replace"]
    # replace 之后仅允许目录项 fsync（Windows 上目录 fsync 被容错跳过则不出现）
    assert set(events[2:]) <= {"fsync"}
    assert target.exists()


def test_atomic_write_replace_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟断电：os.replace 抛异常时原文件不动 临时文件清理干净"""
    target = tmp_path / "store.json"
    target.write_text("original", encoding="utf-8")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated power cut")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated power cut"):
        write_text_atomic(target, "new-content")

    assert target.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.rglob("*.tmp"))


def test_reminder_store_quarantines_corrupt_file_and_blocks_save(tmp_path: Path) -> None:
    """坏 JSON 改名 .corrupt 保留 且损坏保护态下 save 跳过 防止空集覆写"""
    path = tmp_path / "reminders" / "store.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    store = ReminderStore(path)
    assert store.load() == {}
    corrupt = path.with_name("store.json.corrupt")
    assert corrupt.read_text(encoding="utf-8") == "{not valid json"
    assert not path.exists()

    # 损坏保护态：save 跳过且告警 不会把空集写回
    store.save({})
    assert not path.exists()
    assert corrupt.read_text(encoding="utf-8") == "{not valid json"

    # 再次损坏时覆盖旧的 .corrupt
    path.write_text("[broken", encoding="utf-8")
    assert ReminderStore(path).load() == {}
    assert corrupt.read_text(encoding="utf-8") == "[broken"

    # 修复文件后新实例（相当于重启）恢复正常读写
    path.write_text(json.dumps({"reminders": []}), encoding="utf-8")
    restored = ReminderStore(path)
    assert restored.load() == {}
    restored.save({})
    assert json.loads(path.read_text(encoding="utf-8")) == {"reminders": []}


def test_reminder_store_oserror_does_not_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#337 瞬时 OS 错误（文件锁/杀软）不隔离健康文件 不锁死本实例写入"""
    path = tmp_path / "reminders" / "store.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"reminders": []}), encoding="utf-8")

    def locked_read_text(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("simulated antivirus file lock")

    monkeypatch.setattr(Path, "read_text", locked_read_text)
    store = ReminderStore(path)
    assert store.load() == {}
    monkeypatch.undo()

    # 健康文件未被改名 .corrupt 本实例仍可写入
    assert path.exists()
    assert not path.with_name("store.json.corrupt").exists()
    assert store.save({}) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"reminders": []}


def test_reminder_store_save_returns_bool(tmp_path: Path) -> None:
    """#337 save 返回落盘结果 损坏保护态返回 False"""
    path = tmp_path / "reminders" / "store.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    guarded = ReminderStore(path)
    assert guarded.load() == {}
    assert guarded.save({}) is False

    healthy = ReminderStore(tmp_path / "reminders2" / "store.json")
    assert healthy.save({}) is True


def test_atomic_write_tolerates_dir_fsync_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#344 fsync 失败（内容或目录项 网络盘/Windows）不影响写入"""
    target = tmp_path / "store.json"

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated network drive")

    # 只 patch fsync：patch os.open 会误伤 mkstemp 的内部打开
    monkeypatch.setattr(os, "fsync", boom)
    write_text_atomic(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_reminder_store_accepts_legacy_list_shape(tmp_path: Path) -> None:
    path = tmp_path / "reminders" / "store.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {
                    "id": "once_1",
                    "kind": "one_time",
                    "message": "hi",
                    "next_run_at": "2099-01-01T00:00:00+08:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert ReminderStore(path).load()["once_1"].message == "hi"


def test_read_tail_text_reads_end_without_full_file(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    path.write_text(("line\n" * 5000) + "TAIL_MARKER", encoding="utf-8")
    text = read_tail_text(path, max_chars=32)
    assert "TAIL_MARKER" in text
    assert len(text) <= 80


class _StubScheduler(TickSchedulerBase[ReminderRecord]):
    def __init__(self) -> None:
        self.persisted = False
        super().__init__(enqueue=lambda *_: None, coara_id="test", scheduler_label="Stub")

    def _load_persisted_records(self) -> dict[str, ReminderRecord]:
        return {}

    def _persist_records(self) -> None:
        self.persisted = True

    def _normalize_loaded_records(self) -> None:
        pass

    async def _tick(self) -> None:
        pass


@pytest.mark.asyncio
async def test_tick_scheduler_base_stop_persists() -> None:
    sched = _StubScheduler()
    await sched.start()
    await sched.stop()
    assert sched.persisted


# --- stale matters card purge ---


def test_purge_stale_matters_card_files_removes_store_and_runs(tmp_path: Path) -> None:
    matters = tmp_path / "users" / "default" / "matters"
    matters.mkdir(parents=True)
    store = matters / "store.json"
    corrupt = matters / "store.json.corrupt"
    runs = matters / "runs.jsonl"
    keep_def = matters / "definitions" / "cron.yaml"
    keep_def.parent.mkdir(parents=True)
    keep_state = matters / ".state" / "x.json"
    keep_state.parent.mkdir(parents=True)
    store.write_text("{}", encoding="utf-8")
    corrupt.write_text("bad", encoding="utf-8")
    runs.write_text("{}\n", encoding="utf-8")
    keep_def.write_text("id: x\n", encoding="utf-8")
    keep_state.write_text("{}", encoding="utf-8")

    removed = purge_stale_matters_card_files(tmp_path)
    assert {p.name for p in removed} == {"store.json", "store.json.corrupt", "runs.jsonl"}
    assert not store.exists() and not corrupt.exists() and not runs.exists()
    assert keep_def.is_file() and keep_state.is_file()
    # idempotent
    assert purge_stale_matters_card_files(tmp_path) == []


def test_ensure_user_layout_purges_stale_matters_card_files(tmp_path: Path) -> None:
    matters = user_paths(tmp_path).matters_dir
    matters.mkdir(parents=True)
    leftover = matters / "store.json"
    leftover.write_text("{}", encoding="utf-8")
    ensure_user_layout(tmp_path)
    assert not leftover.exists()
    assert user_paths(tmp_path).matters_definitions_dir.is_dir()


# --- test isolation ---


def test_bootstrap_resolves_to_isolated_home(isolated_coara_home: Path) -> None:
    assert resolve_bootstrap_coara_home() == isolated_coara_home.resolve()


def test_ensure_workspace_layout_writes_under_isolated_home(
    isolated_coara_home: Path,
    tmp_path: Path,
) -> None:
    ensure_workspace_layout(tmp_path)
    workspaces_root = isolated_coara_home / "workspaces"
    assert workspaces_root.is_dir()
    created = list(workspaces_root.iterdir())
    assert len(created) == 1
    assert created[0].resolve().is_relative_to(isolated_coara_home.resolve())


@pytest.mark.asyncio
async def test_config_manager_load_uses_isolated_home(isolated_coara_home: Path) -> None:
    await config_manager.load()
    assert config_manager.config.coara_home == isolated_coara_home.resolve()
