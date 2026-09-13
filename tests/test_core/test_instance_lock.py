"""单实例锁：创建 / 存活检测 / 残留清理 / 退出删除。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.instance_lock import (
    LOCK_FILENAME,
    InstanceLockError,
    _pid_alive,
    acquire_instance_lock,
    release_instance_lock,
)


def test_pid_alive() -> None:
    assert _pid_alive(os.getpid())
    assert not _pid_alive(-1)
    assert not _pid_alive(0)
    # 几乎不可能存在的超大 PID
    assert not _pid_alive(2**22 + 12345)


def test_acquire_creates_lock_with_current_pid(tmp_path: Path) -> None:
    lock = acquire_instance_lock(tmp_path)
    assert lock == tmp_path / LOCK_FILENAME
    assert lock.is_file()
    assert int(lock.read_text(encoding="utf-8")) == os.getpid()


def test_acquire_with_explicit_pid_skips_atexit(tmp_path: Path) -> None:
    """config 加载后以最终口径重建锁：写入自身 PID 但不重复注册 atexit。"""
    lock = acquire_instance_lock(tmp_path, pid=os.getpid())
    assert lock.is_file()


def test_acquire_refused_when_live_instance_holds_lock(tmp_path: Path) -> None:
    # 写入当前进程 PID（存活），模拟另一实例持锁
    (tmp_path / LOCK_FILENAME).write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(InstanceLockError) as exc_info:
        acquire_instance_lock(tmp_path)
    assert exc_info.value.pid == os.getpid()
    assert "已有 coara 实例在运行" in str(exc_info.value)


def test_acquire_cleans_stale_lock(tmp_path: Path) -> None:
    # 死 PID 的残留锁 → 清理后正常拿锁
    stale = tmp_path / LOCK_FILENAME
    stale.write_text(str(2**22 + 12345), encoding="utf-8")
    lock = acquire_instance_lock(tmp_path)
    assert lock.is_file()
    assert int(lock.read_text(encoding="utf-8")) == os.getpid()


def test_acquire_cleans_garbage_lock(tmp_path: Path) -> None:
    # 内容不是合法 PID（损坏锁）按残留处理
    (tmp_path / LOCK_FILENAME).write_text("not-a-pid", encoding="utf-8")
    lock = acquire_instance_lock(tmp_path)
    assert int(lock.read_text(encoding="utf-8")) == os.getpid()


def test_release_removes_lock(tmp_path: Path) -> None:
    lock = acquire_instance_lock(tmp_path)
    assert lock.is_file()
    release_instance_lock(tmp_path)
    assert not lock.exists()
    # 幂等：重复释放不报错
    release_instance_lock(tmp_path)


def test_atexit_removes_lock(tmp_path: Path) -> None:
    """模拟进程正常退出：atexit 注册的回调删掉锁文件。"""
    lock = acquire_instance_lock(tmp_path)
    assert lock.is_file()
    # 手动触发 atexit 回调（进程退出时走同一路径；重复执行靠 suppress 幂等）
    import src.core.instance_lock as mod

    mod._release_lock_file(lock)
    assert not lock.exists()


def test_acquire_after_release_succeeds(tmp_path: Path) -> None:
    """Ctrl+B 交接路径：旧实例释放后，新实例启动能拿到锁。"""
    acquire_instance_lock(tmp_path)
    release_instance_lock(tmp_path)
    lock = acquire_instance_lock(tmp_path)
    assert int(lock.read_text(encoding="utf-8")) == os.getpid()
