"""supervisor 模式重启：意图传递（runtime/restart）与退出码分派（cli/supervisor）。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from src.cli import supervisor
from src.runtime import restart


@pytest.fixture(autouse=True)
def _reset_restart_state():
    """模块级意图标志跨用例隔离。"""
    restart._restart_requested = False
    restart._stop_event = None
    restart._stop_loop = None
    yield
    restart._restart_requested = False
    restart._stop_event = None
    restart._stop_loop = None


def test_request_restart_sets_flag_and_writes_record(tmp_path: Path) -> None:
    restart.request_restart(None, reason="manual", requested_by="owner", home=tmp_path)
    assert restart.consume_restart_flag() is True
    # 消费后归零：重复读取不会二次重启
    assert restart.consume_restart_flag() is False
    record = json.loads((tmp_path / "restart_last.json").read_text(encoding="utf-8"))
    assert record["reason"] == "manual"
    assert record["requested_by"] == "owner"
    assert record["pid"] > 0


def test_request_restart_without_home_still_sets_flag() -> None:
    restart.request_restart(None, reason="manual", requested_by="owner", home=None)
    assert restart.consume_restart_flag() is True


def test_restart_divider_handoff_round_trip_and_idempotent(tmp_path: Path) -> None:
    """重启交接：记录带空间/会话归属，consume 一次画线后标 divider_done 幂等。"""
    from types import SimpleNamespace

    view = SimpleNamespace(workspace_dir="D:/ws/demo", session_id="sess-1")
    root = SimpleNamespace(
        resolve_web_view_coara=lambda: view,
        foreground_coara=view,
    )
    restart.request_restart(root, reason="manual", requested_by="owner", home=tmp_path)

    handoff = restart.consume_restart_divider(tmp_path)
    assert handoff == {"workspace_dir": "D:/ws/demo", "session_id": "sess-1"}

    # 幂等：再消费返回 None（防 respawn 循环/反复启动刷线）
    assert restart.consume_restart_divider(tmp_path) is None
    record = json.loads((tmp_path / "restart_last.json").read_text(encoding="utf-8"))
    assert record["divider_done"] is True


def test_restart_divider_no_record_returns_none(tmp_path: Path) -> None:
    """非 /restart 退出（信号/托盘/崩溃）无交接记录：启动不画线。"""
    assert restart.consume_restart_divider(tmp_path) is None


def test_request_restart_wakes_bound_stop_event() -> None:
    async def _run() -> bool:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        restart.bind_restart_stop_event(stop, loop)
        restart.request_restart(None, reason="manual", requested_by="owner")
        await asyncio.sleep(0)
        return stop.is_set()

    assert asyncio.run(_run()) is True


def test_bind_resets_pending_flag() -> None:
    """新内核启动时残留意图必须清零：上一轮的标志不该带进新进程语义。"""
    restart._restart_requested = True

    async def _run() -> None:
        restart.bind_restart_stop_event(asyncio.Event(), asyncio.get_running_loop())

    asyncio.run(_run())
    assert restart.consume_restart_flag() is False


class _FakeProc:
    """可控退出码的 Popen 替身：wait/poll 返回预设码。"""

    def __init__(self, code: int) -> None:
        self.pid = 9999
        self._code = code
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self._code

    def poll(self) -> int:
        return self._code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_supervisor_respawns_on_restart_code(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[int] = []

    def fake_spawn(workspace: Path, home: Path | None) -> _FakeProc:
        # 第一次以 42（重启）退出，第二次干净退出 → supervisor 应 respawn 一次
        spawned.append(len(spawned))
        return _FakeProc(supervisor.EXIT_RESTART if len(spawned) == 1 else 0)

    monkeypatch.setattr(supervisor, "_spawn_kernel", fake_spawn)
    code = supervisor.run_supervisor(Path.cwd(), None, with_tray=False)
    assert code == 0
    assert len(spawned) == 2


def test_supervisor_exits_on_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[int] = []

    def fake_spawn(workspace: Path, home: Path | None) -> _FakeProc:
        spawned.append(1)
        return _FakeProc(0)

    monkeypatch.setattr(supervisor, "_spawn_kernel", fake_spawn)
    assert supervisor.run_supervisor(Path.cwd(), None, with_tray=False) == 0
    assert len(spawned) == 1  # 不 respawn


def test_supervisor_crashes_backoff_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    spawned: list[int] = []

    def fake_spawn(workspace: Path, home: Path | None) -> _FakeProc:
        spawned.append(1)
        return _FakeProc(1 if len(spawned) == 1 else 0)

    monkeypatch.setattr(supervisor, "_spawn_kernel", fake_spawn)
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: delays.append(s))
    # 崩溃实例 uptime=0（立即挂）→ 触发首档退避
    assert supervisor.run_supervisor(Path.cwd(), None, with_tray=False) == 0
    assert len(spawned) == 2
    assert delays == [supervisor._CRASH_BACKOFF_S[0]]


def test_supervisor_kernel_argv_uses_internal_command(tmp_path: Path) -> None:
    argv = supervisor._kernel_argv(tmp_path)
    assert argv[-1] == "_kernel"
    assert "--workspace" in argv
    assert str(tmp_path) in argv


def test_supervisor_spawn_injects_supervised_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProc:
        captured["argv"] = argv
        captured.update(kwargs)
        return _FakeProc(0)

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    supervisor._spawn_kernel(tmp_path, tmp_path)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["COARA_SUPERVISED"] == "1"
    # 内核输出落 daemon.log，崩溃才有堆栈可查
    assert (tmp_path / "logs" / "daemon.log").exists()


def test_supervisor_spawn_hides_console_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DETACHED supervisor 拉内核必须 CREATE_NO_WINDOW，否则每次 respawn 弹 python.exe。"""
    captured: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc(0)

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor.sys, "platform", "win32")
    supervisor._spawn_kernel(tmp_path, tmp_path)
    flags = int(captured["creationflags"])  # type: ignore[arg-type]
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert flags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def test_open_mobile_loads_config_before_reading_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """托盘「连接手机」必须先加载 config：否则 _gomatrix_port 回落默认 8008，
    实际服务在别的端口时二维码永远拿不到（静默回落打开 web 页）。"""
    from src.coara.commands import qrcode
    from src.core.config import config_manager

    monkeypatch.setattr(config_manager, "_config", None)
    loaded: list[bool] = []

    async def fake_load() -> None:
        loaded.append(True)
        config_manager._config = object()  # 标记已加载即可，端口读取已被隔离

    monkeypatch.setattr(config_manager, "load", fake_load)
    monkeypatch.setattr(qrcode, "_dashboard_status", lambda: None)
    # fallback（open_web）会连带触发，这里隔断它，只看 _make_open_mobile 自身行为
    monkeypatch.setattr(supervisor, "_make_open_web", lambda _w, _h: lambda: None)

    supervisor._make_open_mobile(tmp_path, tmp_path)()
    assert loaded, "_make_open_mobile 未确保 config 加载就去读 matrix 端口"


def test_terminate_kernel_graceful_then_kill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proc = _FakeProc(0)
    monkeypatch.setattr(supervisor, "_request_graceful_shutdown", lambda _w, _h: True)
    supervisor._terminate_kernel(proc, tmp_path, tmp_path)  # 已退出：不动
    assert not proc.terminated and not proc.killed

    # 优雅端点受理 + 进程在宽限内退出 → 不走 terminate
    proc2 = _FakeProc(0)
    proc2._code = None  # type: ignore[assignment]  # poll 未见退出 → 走停机分支

    class _PendingThenExit(_FakeProc):
        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    proc3 = _PendingThenExit(0)
    supervisor._terminate_kernel(proc3, tmp_path, tmp_path)
    assert not proc3.terminated and not proc3.killed


def test_wake_stop_event_without_bind() -> None:
    assert restart.wake_stop_event() is False


def test_wake_stop_event_wakes_bound() -> None:
    async def _run() -> bool:
        restart.bind_restart_stop_event(asyncio.Event(), asyncio.get_running_loop())
        assert restart.wake_stop_event() is True
        await asyncio.sleep(0)
        return True

    assert asyncio.run(_run()) is True


def test_graceful_shutdown_endpoint_loopback_gate() -> None:
    """优雅停机端点：非回环地址一律 403（token 之外的第二道闸）。"""
    from unittest.mock import MagicMock

    from src.ui.web_server import WebServer

    server = WebServer.__new__(WebServer)
    request = MagicMock()
    request.remote = "192.168.1.50"

    resp = asyncio.run(server._handle_kernel_shutdown(request))
    assert resp.status == 403


def test_graceful_shutdown_endpoint_wakes_stop_event() -> None:
    """优雅停机端点：回环 + token 通过 → 唤醒 stop_event（与信号退出同一条路径）。"""
    from unittest.mock import MagicMock, patch

    from src.ui.web_server import WebServer

    async def _run() -> tuple[int, bool]:
        stop = asyncio.Event()
        restart.bind_restart_stop_event(stop, asyncio.get_running_loop())
        server = WebServer.__new__(WebServer)
        request = MagicMock()
        request.remote = "127.0.0.1"
        with patch.object(WebServer, "_check_token", lambda _self, _req: None):
            resp = await server._handle_kernel_shutdown(request)
        await asyncio.sleep(0)
        return resp.status, stop.is_set()

    status, woke = asyncio.run(_run())
    assert status == 200
    assert woke is True
