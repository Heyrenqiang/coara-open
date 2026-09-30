"""内核 supervisor：spawn → wait → 按退出码分派的薄托管进程。

`coara tray` / `coara daemon` 的入口改为 supervisor：内核作为它的子进程跑
`_kernel` 内部命令，进程生命周期（重启 / 崩溃自愈 / 托盘退出）全部由
supervisor 承担。托盘图标挂在 supervisor 上——重启期间内核会死一次，
托盘不该闪断。

职责严格收口：
- spawn 内核子进程并 wait 退出码
- EXIT_RESTART（42）→ respawn；0 → supervisor 跟着退；其余 → 退避 respawn
- 托盘「退出」→ POST 内核优雅停机端点（无响应才 terminate 兜底）→ supervisor 退

不含 asyncio、不碰 LLM、不做健康检查轮询——内核就绪与否由客户端拉起链
（_spawn_detached_daemon + _wait_daemon_ready 读 active.json + 端口）判定。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 内核以「我要重启」语义退出时用此退出码。supervisor 只在收到它时立即 respawn；
# 0 视为正常退出（supervisor 跟着退），其余非零视为崩溃（退避 respawn 自愈）。
EXIT_RESTART = 42

# 崩溃自愈退避（秒）：5 → 10 → 30 → 之后封顶 5 分钟，防崩溃循环刷爆日志。
_CRASH_BACKOFF_S = (5.0, 10.0, 30.0)
_CRASH_BACKOFF_MAX_S = 300.0
# 跑满此时长才退出的视为偶发崩溃，退避清零重来；起来就挂的逐档加。
_CRASH_WINDOW_S = 60.0
# 托盘退出时等内核优雅收尾的上限；超时强杀。优雅收尾要 drain matrix 游标、
# flush trace/用量、关各服务，5s 偏紧会把它逼成硬杀，给 10s。
_TERMINATE_GRACE_S = 10.0


def _kernel_argv(workspace: Path) -> list[str]:
    """内核子进程命令：固定内部命令 `_kernel`，由 supervisor 专属拉起。"""
    return [sys.executable, "-m", "src.coara", "--workspace", str(workspace), "_kernel"]


def _spawn_kernel(workspace: Path, home: Path | None) -> subprocess.Popen:
    """拉起内核子进程；stdout/stderr 落 daemon.log，注入 COARA_SUPERVISED=1。"""
    log_stream = None
    if home is not None:
        try:
            log_dir = home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_stream = open(log_dir / "daemon.log", "ab")  # noqa: SIM115 — 句柄由子进程继承
        except OSError:
            log_stream = None
    kwargs: dict = {
        "cwd": str(workspace),
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream if log_stream is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if log_stream is not None else subprocess.DEVNULL,
        "close_fds": True,
        "env": {**os.environ, "COARA_SUPERVISED": "1", "PYTHONUNBUFFERED": "1"},
    }
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP：托盘退出时 terminate 能送达内核信号处理。
        # CREATE_NO_WINDOW：supervisor 自身是 DETACHED（无控制台），不带此旗标时
        # Windows 会给每次 spawn 的 python.exe 新开可见控制台——关窗即
        # STATUS_CONTROL_C_EXIT(0xC000013A)，supervisor 退避 respawn → 弹窗死循环。
        # 托盘图标挂在 supervisor 上，内核无 NotifyIcon，此处可安全隐藏窗口。
        from src.utils.win_proc import no_window_creationflags

        kwargs["creationflags"] = no_window_creationflags(subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(_kernel_argv(workspace), **kwargs)  # noqa: S603 — argv 由本进程构造
    finally:
        if log_stream is not None:
            with contextlib.suppress(OSError):
                log_stream.close()


def _terminate_kernel(proc: subprocess.Popen, workspace: Path, home: Path | None) -> None:
    """托盘退出路径：先走内核的优雅停机端点（trace/游标/锁自然收尾），
    端点无响应才 terminate 兜底，再不行强杀。"""
    if proc.poll() is not None:
        return
    if _request_graceful_shutdown(workspace, home):
        try:
            proc.wait(timeout=_TERMINATE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=2.0)


def _request_graceful_shutdown(workspace: Path, home: Path | None) -> bool:
    """POST /api/v1/kernel/shutdown（本机 + token 鉴权）。拿到 stopping=true 视为已受理。"""
    import httpx

    try:
        from src.ui.dashboard_tokens import load_or_create_dashboard_token

        token = load_or_create_dashboard_token(workspace, home) if home else ""
    except Exception:
        return False
    if not token:
        return False
    port = int(os.environ.get("COARA_WEB_PORT", "8080"))
    try:
        with httpx.Client(trust_env=False, timeout=2.0) as client:
            resp = client.post(f"http://127.0.0.1:{port}/api/v1/kernel/shutdown?token={token}")
        return resp.status_code == 200 and bool(resp.json().get("stopping"))
    except Exception:
        return False


def run_supervisor(workspace: Path, home: Path | None, *, with_tray: bool) -> int:
    """supervisor 主循环。返回 supervisor 自身退出码（托盘退出 / 内核正常退出时为 0）。"""
    from src.core.logger import logger

    quit_event = threading.Event()
    launcher = None
    if with_tray:
        from src.cli.tray import run_tray

        launcher = run_tray(
            open_web=_make_open_web(workspace, home),
            open_mobile=_make_open_mobile(workspace, home),
            quit_kernel=quit_event.set,
        )
        if launcher is None:
            from src.cli.main import console

            console.print("[yellow]未检测到 pystray，托盘不可用；以纯 supervisor 运行。[/yellow]")

    crash_backoff_idx = 0
    try:
        while True:
            proc = _spawn_kernel(workspace, home)
            logger.info(f"[supervisor] kernel spawned pid={proc.pid}")
            spawn_at = time.monotonic()
            while True:
                if quit_event.is_set():
                    _terminate_kernel(proc, workspace, home)
                    logger.info("[supervisor] tray quit; kernel stopped, supervisor exiting")
                    return 0
                code = proc.poll()
                if code is not None:
                    break
                time.sleep(0.3)
            uptime = time.monotonic() - spawn_at

            if code == EXIT_RESTART:
                logger.info("[supervisor] kernel requested restart; respawning")
                crash_backoff_idx = 0
                continue
            if code == 0:
                logger.info("[supervisor] kernel exited cleanly; supervisor exiting")
                return 0

            if uptime >= _CRASH_WINDOW_S:
                crash_backoff_idx = 0
            delay = _CRASH_BACKOFF_S[min(crash_backoff_idx, len(_CRASH_BACKOFF_S) - 1)]
            delay = min(delay, _CRASH_BACKOFF_MAX_S)
            crash_backoff_idx += 1
            logger.warning(
                f"[supervisor] kernel exited abnormally (code={code}, uptime={uptime:.1f}s); "
                f"respawn in {delay:.0f}s"
            )
            time.sleep(delay)
    finally:
        if launcher is not None:
            launcher.stop()


def _make_open_web(workspace: Path, home: Path | None):
    def _open() -> None:
        from src.core.logger import logger
        from src.ui.dashboard_tokens import load_or_create_dashboard_token
        from src.ui.web_server import open_or_focus_web_ui

        try:
            try:
                token = load_or_create_dashboard_token(workspace, home) if home else ""
            except Exception:
                token = ""
            port = int(os.environ.get("COARA_WEB_PORT", "8080"))
            # 无任何可用模型时直落配置页「模型」：新用户装完第一屏就是填 key 的地方。
            # 注意：supervisor 进程里 config_manager 默认从未加载过配置（加载只在内核进程
            # 里发生），不先 load 就读 provider 列表永远为空 → 每次点托盘都误判成无模型弹
            # 配置页（09-25 回归）。这里先确保加载完成再判；加载失败按「有模型」不打扰用户。
            path = "/"
            try:
                from src.core.config import config_manager as _cfg_mgr
                from src.llm.model_catalog import _enabled_provider_names

                if getattr(_cfg_mgr, "_config", None) is None:
                    import asyncio

                    asyncio.run(_cfg_mgr.load())
                if not _enabled_provider_names(_cfg_mgr):
                    path = "/config?focus=models"
            except Exception:
                path = "/"
            open_or_focus_web_ui(host="127.0.0.1", port=port, token=token, path=path)
        except Exception as exc:
            logger.exception(f"[supervisor] tray open web failed: {exc}")

    return _open


def _make_open_mobile(workspace: Path, home: Path | None):
    def _open() -> None:
        import tempfile

        from src.coara.commands.qrcode import _dashboard_status, _fetch_bytes, _gomatrix_port

        # supervisor 进程里 config_manager 默认未加载（加载只在内核进程发生）：
        # 不先 load，_gomatrix_port 读不到 matrix.port 会回落默认 8008，实际服务在
        # 别的端口时二维码永远拿不到（与 _make_open_web 的 09-25 回归同坑）。
        try:
            from src.core.config import config_manager as _cfg_mgr

            if getattr(_cfg_mgr, "_config", None) is None:
                import asyncio

                asyncio.run(_cfg_mgr.load())
        except Exception:
            pass
        fallback = _make_open_web(workspace, home)
        status = _dashboard_status()
        png = b""
        if status is not None and status.get("tunnel_ready") and status.get("tunnel_url"):
            png = _fetch_bytes(f"http://127.0.0.1:{_gomatrix_port()}/dashboard/qr.png") or b""
        if not png:
            fallback()
            return
        try:
            tmp = Path(tempfile.gettempdir()) / "coara-mobile-qr.png"
            tmp.write_bytes(png)
            if sys.platform == "win32":
                os.startfile(str(tmp))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(tmp)])  # noqa: S603,S607
        except OSError:
            fallback()

    return _open
