"""裸 coara 启动重构：内核生命周期独立于终端。

终端永远只是端。无内核 → spawn 独立 daemon（脱离终端）+ 等就绪 + attach；
有内核 → 直接 attach。终端关闭不杀内核。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cli import main as cli_main

pytestmark = pytest.mark.extended


def _runtime(pid: int = 1234, name: str = "nx"):
    return SimpleNamespace(pid=pid, workspace_name=name)


def _patch_port_ok(monkeypatch, *, ok: bool = True) -> None:
    """模拟 Web 端口可连 / 不可连（半死内核）。"""
    import socket

    if ok:
        conn = MagicMock()
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: conn)
    else:

        def _fail(*_a, **_k):
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", _fail)


def test_attach_when_kernel_already_running(monkeypatch, tmp_path) -> None:
    """内核在跑：直接 attach，不 spawn。"""
    called = {"attach": None, "spawn": 0}
    # load_active_runtime 在函数内 import，patch 源模块
    import src.coara.workspace_runtime as wr

    monkeypatch.setattr(wr, "load_active_runtime", lambda home: _runtime())
    monkeypatch.setattr(cli_main, "_spawn_detached_daemon", lambda ws: called.__setitem__("spawn", called["spawn"] + 1))
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        cli_main,
        "_attach_into_running_instance",
        lambda ws, *, running_pid, running_workspace: called.__setitem__("attach", (running_pid, running_workspace)),
    )
    _patch_port_ok(monkeypatch, ok=True)

    cli_main._ensure_kernel_and_attach(tmp_path)

    assert called["spawn"] == 0
    assert called["attach"] == (1234, "nx")


def test_spawn_daemon_and_attach_when_no_kernel(monkeypatch, tmp_path) -> None:
    """无内核：spawn daemon + 等就绪 + attach。"""
    import src.coara.workspace_runtime as wr

    seq = {"calls": 0}

    def fake_load(home):
        # 第一次（spawn 前）无内核；wait 后再查（attach 前）有内核
        seq["calls"] += 1
        return None if seq["calls"] == 1 else _runtime(pid=5555, name="v8")

    monkeypatch.setattr(wr, "load_active_runtime", fake_load)
    monkeypatch.setattr(wr, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    spawned = {}
    monkeypatch.setattr(cli_main, "_spawn_detached_daemon", lambda ws: spawned.setdefault("pid", 5555))
    monkeypatch.setattr(cli_main, "_wait_daemon_ready", lambda ws, **kw: True)
    attached = {}
    monkeypatch.setattr(
        cli_main,
        "_attach_into_running_instance",
        lambda ws, *, running_pid, running_workspace: attached.update(pid=running_pid, ws=running_workspace),
    )

    cli_main._ensure_kernel_and_attach(tmp_path)

    assert spawned["pid"] == 5555
    assert attached["pid"] == 5555
    assert attached["ws"] == "v8"


def test_half_dead_waits_for_exit_then_spawns(monkeypatch, tmp_path) -> None:
    """PID 活着但 Web 不通：等旧进程退出后再 spawn，勿立刻清登记撞锁。"""
    import src.coara.workspace_runtime as wr

    loads = {"n": 0}
    cleared = {"pid": None}

    def fake_load(home):
        loads["n"] += 1
        # 1: 半死探测有登记；2: spawn 就绪后再查
        if loads["n"] == 1:
            return _runtime(pid=9999, name="nx")
        return _runtime(pid=8888, name="nx")

    monkeypatch.setattr(wr, "load_active_runtime", fake_load)
    monkeypatch.setattr(wr, "clear_active_runtime", lambda home, *, pid=None: cleared.update(pid=pid))
    monkeypatch.setattr(wr, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli_main, "_wait_pid_exit", lambda pid, **kw: True)
    monkeypatch.setattr(cli_main, "_read_instance_lock_pid", lambda home: None)
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    _patch_port_ok(monkeypatch, ok=False)
    spawned = {}
    monkeypatch.setattr(cli_main, "_spawn_detached_daemon", lambda ws: spawned.setdefault("pid", 8888))
    monkeypatch.setattr(cli_main, "_wait_daemon_ready", lambda ws, **kw: True)
    attached = {}
    monkeypatch.setattr(
        cli_main,
        "_attach_into_running_instance",
        lambda ws, *, running_pid, running_workspace: attached.update(pid=running_pid),
    )

    cli_main._ensure_kernel_and_attach(tmp_path)

    assert cleared["pid"] == 9999
    assert spawned["pid"] == 8888
    assert attached["pid"] == 8888


def test_half_dead_stuck_exits(monkeypatch, tmp_path) -> None:
    """半死进程一直不退 → 直接报错，不 spawn。"""
    import src.coara.workspace_runtime as wr

    monkeypatch.setattr(wr, "load_active_runtime", lambda home: _runtime(pid=4242))
    monkeypatch.setattr(cli_main, "_wait_pid_exit", lambda pid, **kw: False)
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    _patch_port_ok(monkeypatch, ok=False)
    spawned = {"n": 0}
    monkeypatch.setattr(cli_main, "_spawn_detached_daemon", lambda ws: spawned.__setitem__("n", spawned["n"] + 1))

    with pytest.raises(SystemExit) as exc:
        cli_main._ensure_kernel_and_attach(tmp_path)
    assert exc.value.code == 1
    assert spawned["n"] == 0


def test_spawn_failure_exits(monkeypatch, tmp_path) -> None:
    """spawn daemon 失败（pid=0）→ SystemExit(1)，不 attach。"""
    import src.coara.workspace_runtime as wr

    monkeypatch.setattr(wr, "load_active_runtime", lambda home: None)
    monkeypatch.setattr(wr, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli_main, "_read_instance_lock_pid", lambda home: None)
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_main, "_spawn_detached_daemon", lambda ws: 0)

    with pytest.raises(SystemExit) as exc:
        cli_main._ensure_kernel_and_attach(tmp_path)
    assert exc.value.code == 1


def test_wait_timeout_exits(monkeypatch, tmp_path) -> None:
    """daemon 就绪超时 → SystemExit(1)，不 attach。"""
    import src.coara.workspace_runtime as wr

    monkeypatch.setattr(wr, "load_active_runtime", lambda home: None)
    monkeypatch.setattr(wr, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli_main, "_read_instance_lock_pid", lambda home: None)
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli_main, "_spawn_detached_daemon", lambda ws: 7777)
    monkeypatch.setattr(cli_main, "_wait_daemon_ready", lambda ws, **kw: False)

    with pytest.raises(SystemExit) as exc:
        cli_main._ensure_kernel_and_attach(tmp_path)
    assert exc.value.code == 1


def test_non_tty_exits(monkeypatch, tmp_path) -> None:
    """非 TTY（脚本/管道）→ 报错退出，不 spawn 不 attach。"""
    monkeypatch.setattr(cli_main.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as exc:
        cli_main._ensure_kernel_and_attach(tmp_path)
    assert exc.value.code == 1


def test_spawn_uses_detached_flags_windows(monkeypatch, tmp_path) -> None:
    """_spawn_detached_daemon 在 win32 拉起 tray（含托盘图标）并 detach。"""
    import subprocess

    captured = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli_main.sys, "platform", "win32")
    monkeypatch.setattr(cli_main.sys, "executable", "python")

    pid = cli_main._spawn_detached_daemon(tmp_path)

    assert pid == 4321
    assert captured["cmd"][:3] == ["python", "-m", "src.coara"]
    # 全局 --workspace 必须在子命令 tray 之前（否则 Click 拒收、进程秒退）
    assert captured["cmd"][3:5] == ["--workspace", str(tmp_path)]
    assert captured["cmd"][5] == "tray"
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"].get("PYTHONUNBUFFERED") == "1"
    flags = captured["kwargs"]["creationflags"]
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    # 不加 CREATE_NO_WINDOW：否则部分 Windows 下托盘 NotifyIcon 不显示
    assert not (flags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
