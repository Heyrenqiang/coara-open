"""单实例锁：防止多个 coara 进程共用同一 coara Home 互踩数据。

启动时在 ``<coara_home>/coara.pid`` 以 O_EXCL 写入当前 PID：
- 已存在且 PID 存活 → 拒绝启动（InstanceLockError）
- 已存在但 PID 已死（异常退出残留）→ 清掉旧锁继续

正常退出经 atexit 删锁。强杀（taskkill / 关窗走 console close handler 时
由 shutdown 路径兜底）留下的残留锁会在下次启动时被自动清理。

Ctrl+B 交接安全：交接进程在旧进程退出后才启动，旧锁已删。
"""

from __future__ import annotations

import atexit
import contextlib
import os
from pathlib import Path

LOCK_FILENAME = "coara.pid"


class InstanceLockError(RuntimeError):
    """已有存活的 coara 实例持有同一 coara Home 的锁。"""

    def __init__(self, pid: int, lock_path: Path) -> None:
        self.pid = pid
        self.lock_path = lock_path
        super().__init__(f"已有 coara 实例在运行（PID {pid}），请先退出它。锁文件: {lock_path}")


def _pid_alive(pid: int) -> bool:
    """探测 PID 是否仍存活。

    Windows 上禁止 ``os.kill(pid, 0)``：signal 0 即 ``CTRL_C_EVENT``，
    会向当前控制台进程组广播 Ctrl+C，打断 pytest / 发布脚本。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited = 0x1000
        process_query = 0x0400
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited, False, int(pid))
        if not handle:
            handle = kernel32.OpenProcess(process_query, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 无权限发信号说明进程存在（他人/提权进程）
        return True
    except OSError:
        return False
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    """读取锁文件 PID；读到一半被清空（创建竞态）返回 0，按存活实例处理。"""
    with contextlib.suppress(OSError):
        text = lock_path.read_text(encoding="utf-8", errors="replace").strip()
        if text.isdigit():
            return int(text)
        if not text:
            return 0
    return None


def acquire_instance_lock(coara_home: Path, *, pid: int | None = None) -> Path:
    """在 coara_home 下创建 PID 锁；已有存活实例时抛 InstanceLockError。"""
    coara_home.mkdir(parents=True, exist_ok=True)
    lock_path = coara_home / LOCK_FILENAME
    owner_pid = os.getpid() if pid is None else pid
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            existing = _read_lock_pid(lock_path)
            if existing is not None and (existing == 0 or _pid_alive(existing)):
                raise InstanceLockError(existing, lock_path) from None
            # 残留锁（异常退出 / 强杀 / 内容损坏）：清掉重试
            with contextlib.suppress(OSError):
                lock_path.unlink()
            continue
        try:
            os.write(fd, str(owner_pid).encode("ascii"))
        finally:
            os.close(fd)
        if owner_pid == os.getpid():
            atexit.register(_release_lock_file, lock_path)
        return lock_path
    # 理论上到不了：两轮清理后仍抢不到视为残留清理竞态失败
    raise InstanceLockError(_read_lock_pid(lock_path) or -1, lock_path)


def _release_lock_file(lock_path: Path) -> None:
    with contextlib.suppress(OSError):
        lock_path.unlink()


def release_instance_lock(coara_home: Path) -> None:
    """主动释放（shutdown 路径）；atexit 兜底与之幂等。"""
    _release_lock_file(coara_home / LOCK_FILENAME)
