"""CLI entrypoint for coara."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.prompt import Prompt

from src.cli.matrix_connect import build_matrix_settings, matrix_config_incomplete, prepare_matrix_connection
from src.core.config import config_manager
from src.core.logger import logger, setup_logger

# pythonw（双击桌面图标的 tray 路径）无控制台：stdout/stderr 为 None，
# 任何 print/console 输出都会抛错。先换成丢弃流，再建 Console（显式接管流，
# 避免 Console() 默认抓取时为 None 的 sys.stdout）。
if sys.platform == "win32":
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115 - 进程级接管，不关闭
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115 - 进程级接管，不关闭

console = Console(file=sys.stdout, stderr=False)


def _set_console_title(title: str) -> None:
    """Set the terminal window/tab title (best-effort, never raises)."""
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        if sys.stdout.isatty():
            sys.stdout.write(f"\033]0;{title}\007")
            sys.stdout.flush()


def _setup_asyncio_exception_handler() -> None:
    """Suppress harmless Windows socket errors in asyncio event loop.

    必须在**运行中的**事件循环上调用（如 _run_frontends 内部）——
    asyncio.run 每次都会新建循环，装在外层循环上的过滤器不会生效。
    """

    def exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        # WinError 10054（对端强关）：ProactorEventLoop 在 _call_connection_lost 的
        # sock.shutdown 撞到已断连接，属连接收尾的正常噪声。winerror 在部分路径
        # 取不到（如被包进 ConnectionResetError 的 args），errno 一并兜底。
        if isinstance(exception, OSError):
            code = getattr(exception, "winerror", None) or getattr(exception, "errno", None)
            if code == 10054:
                return
        loop.default_exception_handler(context)

    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().set_exception_handler(exception_handler)


def _spawn_detached_daemon(workspace: Path) -> int:
    """spawn `coara tray` 为脱离终端生命周期的独立常驻进程，返回其 pid。

    内核的生命周期必须独立于任何终端（阶段重构：终端永远只是端，不是内核）。
    拉起 ``tray``（内含 daemon + 系统托盘图标）；无 pystray 时 tray 入口自行
    退化为纯 daemon。Windows 用 CREATE_NO_WINDOW|DETACHED_PROCESS|
    CREATE_NEW_PROCESS_GROUP + start_new_session 彻底 detach；stdin/stdout/stderr
    重定向到日志文件——终端关闭后内核仍存活。返回 0 表示 spawn 失败。
    """
    import subprocess

    home = None
    try:
        from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home

        home = resolve_bootstrap_coara_home() or resolve_coara_home(workspace)
    except Exception:
        home = None
    log_path = (Path(home) / "logs" / "daemon.log") if home else Path(os.devnull)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = open(log_path, "ab")  # noqa: SIM115 — detached 进程持有，父进程不关闭
    except OSError:
        log_stream = open(os.devnull, "ab")  # noqa: SIM115

    cmd = [sys.executable, "-m", "src.coara"]
    # --workspace 是根 cli 的全局选项，必须写在子命令之前：
    #   python -m src.coara --workspace <path> tray
    # 写成 tray --workspace 会被 Click 拒收（No such option），进程秒退，
    # 客户端空等 30s 报「内核启动超时」。
    if workspace:
        cmd += ["--workspace", str(workspace)]
    # tray = 无头内核 + 系统托盘常驻图标（右键开 Web / 手机 / 退出）
    cmd.append("tray")
    # 子进程 stdout/stderr 重定向到文件：无 TTY 时 Python 全缓冲，秒退错误
    # （如 InstanceLock）可能未落盘就丢——强制行缓冲，超时排查才看得到。
    child_env = os.environ.copy()
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream,
        "stderr": log_stream,
        "cwd": str(workspace) if workspace else None,
        "start_new_session": True,
        "close_fds": True,
        "env": child_env,
    }
    if sys.platform == "win32":
        # 托盘图标需要进程能跑 Win32 消息泵；DETACHED + NEW_GROUP 脱离终端即可。
        # 不加 CREATE_NO_WINDOW：该旗标在部分环境下会让 NotifyIcon 不出现在托盘。
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        console.print(f"[red]拉起内核进程失败：{exc}[/red]")
        return 0
    return proc.pid


def _wait_daemon_ready(workspace: Path, *, timeout_s: float = 30.0) -> bool:
    """等 spawn 出的 daemon 就绪：active.json 出现活 pid 且 web 端口可连。"""
    import socket
    import time

    from src.coara.workspace_runtime import load_active_runtime
    from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home

    home = resolve_bootstrap_coara_home() or resolve_coara_home(workspace)
    port = int(os.environ.get("COARA_WEB_PORT", "8080"))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        runtime = load_active_runtime(home)
        if runtime is not None:
            try:
                with socket.create_connection(("localhost", port), timeout=0.5):
                    return True
            except OSError:
                # 有意静默轮询：daemon 就绪前端口未开放是预期状态，0.4s 后重试
                pass
        time.sleep(0.4)
    return False


def _wait_pid_exit(pid: int, *, timeout_s: float = 20.0) -> bool:
    """等指定 PID 退出；超时仍存活返回 False。"""
    import time

    from src.coara.workspace_runtime import is_pid_alive

    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.3)
    return not is_pid_alive(pid)


def _read_instance_lock_pid(home: Path) -> int | None:
    """读 ``<home>/coara.pid``；无文件 / 损坏返回 None。"""
    from src.core.instance_lock import LOCK_FILENAME

    path = Path(home) / LOCK_FILENAME
    if not path.exists():
        return None
    with contextlib.suppress(OSError, ValueError):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.isdigit():
            return int(text)
    return None


def _ensure_kernel_and_attach(workspace: Path) -> None:
    """裸 `coara` 统一入口：确保内核常驻，自己作为 CLI 客户端 attach 接入。

    内核永远只有一个独立常驻进程（托盘或首次 `coara` 拉起的 daemon），终端只是端：
    - 内核在跑（active.json 活 pid + web 端口可连）→ 直接 attach 接入
    - 无内核 / 僵尸登记 / 端口不通 → spawn 独立 tray/daemon 等就绪后 attach

    终端关闭只是客户端断开，内核继续常驻（web/手机不受影响）。
    非 TTY（脚本/管道）无交互意义，报错退出。
    """
    import socket

    from src.coara.workspace_runtime import clear_active_runtime, is_pid_alive, load_active_runtime
    from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home

    if not sys.stdin.isatty():
        console.print("[red]coara 需要交互终端运行（请直接在终端输入 coara）。[/red]")
        raise SystemExit(1)

    home = resolve_bootstrap_coara_home() or resolve_coara_home(workspace)
    port = int(os.environ.get("COARA_WEB_PORT", "8080"))
    running = load_active_runtime(home)
    if running is not None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            # 半死：登记还在、进程可能仍活，但 Web 已关（关停中 / 崩溃残留）。
            # 若立刻清登记并 spawn，会撞上仍持有 coara.pid 的旧进程 → 子进程
            # InstanceLock 秒退，客户端却空等 30s 报「启动超时」。
            stale_pid = running.pid
            console.print(f"[yellow]内核进程 {stale_pid} 无响应（Web 未监听），等待其退出…[/yellow]")
            if not _wait_pid_exit(stale_pid, timeout_s=20.0):
                console.print(
                    f"[red]内核无响应（PID {stale_pid}）。请结束该进程后重试。日志：{home}/logs/daemon.log[/red]"
                )
                raise SystemExit(1) from None
            clear_active_runtime(home, pid=stale_pid)
            running = None

    if running is None:
        # active.json 已无，但锁文件仍指向存活进程（关停末段清了登记未放锁）：
        # 同样先等其退出，再 spawn。
        lock_pid = _read_instance_lock_pid(home)
        if lock_pid is not None and is_pid_alive(lock_pid):
            console.print(f"[yellow]检测到残留内核进程 {lock_pid}，等待其退出…[/yellow]")
            if not _wait_pid_exit(lock_pid, timeout_s=20.0):
                console.print(
                    f"[red]残留内核未退出（PID {lock_pid}）。请结束该进程后重试。日志：{home}/logs/daemon.log[/red]"
                )
                raise SystemExit(1) from None

        pid = _spawn_detached_daemon(workspace)
        if pid == 0:
            raise SystemExit(1)
        if not _wait_daemon_ready(workspace):
            console.print(f"[red]内核启动超时（30s 未就绪）。查看日志：{home}/logs/daemon.log[/red]")
            raise SystemExit(1)
        running = load_active_runtime(home)

    _attach_into_running_instance(
        workspace,
        running_pid=running.pid if running else 0,
        running_workspace=running.workspace_name if running else "",
    )


def _acquire_instance_lock_or_exit(workspace: Path) -> None:
    """内核宿主（daemon / tray）入口：抢单实例锁，抢不到报错退出。

    一台电脑一个内核。启动前已查 active.json 拒重；这里是检查-拉起竞态的互斥兜底。
    """
    from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home
    from src.core.instance_lock import InstanceLockError, acquire_instance_lock

    home = resolve_bootstrap_coara_home() or resolve_coara_home(workspace)
    try:
        acquire_instance_lock(home)
    except InstanceLockError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    global _instance_lock_home
    _instance_lock_home = home


def _attach_into_running_instance(workspace: Path, *, running_pid: int, running_workspace: str) -> None:
    """当前目录登记为工作空间，经 /ws/attach 接入常驻内核，跑完整 CLI 界面。

    cwd 即工作空间（与「cwd 即空间」语义一致）：已登记用现有条目，未登记
    经 ensure_workspace 落注册表。同空间允许多条 attach（各端独立、按连接回投）。

    内核化「原 CLI 界面召回」：不再走 attach 瘦客户端——WsAttachTransport
    连 /ws/attach 后建 RootShim（attached 快照已应用），交给
    run_attached_chat_session 跑原主 CLI 的完整交互界面骨架。
    """
    from src.cli.attach_client import load_attach_token, resolve_web_host_port
    from src.cli.workspace_cmds import _open_cli_registry

    try:
        registry = _open_cli_registry(workspace)
        entry = registry.ensure_workspace(workspace)
    except Exception as exc:
        console.print(f"[red]解析当前目录为工作空间失败：{exc}[/red]")
        raise SystemExit(1) from None

    host, port = resolve_web_host_port()
    try:
        token = load_attach_token(workspace)
    except Exception as exc:
        console.print(f"[red]读取接入凭证失败：{exc}[/red]")
        raise SystemExit(1) from None
    code = _run_attached_chat_command(entry.name, workspace_dir=workspace, host=host, port=port, token=token)
    if code:
        raise SystemExit(code)
    raise SystemExit(0)


def _run_attached_chat_command(
    workspace: str,
    *,
    workspace_dir: Path,
    host: str,
    port: int,
    token: str,
) -> int:
    """裸 coara 主路径：建 WsAttachTransport 连 /ws/attach → RootShim → 完整界面。

    与 attach 瘦客户端（attach_client.run_attach_command，保留给显式
    ``coara attach <ws>``）同一数据通道，但界面是原主 CLI 骨架。返回进程
    退出码。注意：界面正常退出经其内部看门狗硬退（os._exit），本函数只在
    连接/握手失败时返回非零。
    """
    from src.cli.attach_transport import WsAttachTransport
    from src.cli.attached_chat_runner import run_attached_chat_session
    from src.cli.root_shim import RootShim

    # 启动静默：客户端不刷配置加载的 DEBUG/INFO（同 attach 瘦客户端）。
    try:
        from src.core.logger import logger as _logger

        _logger.remove()
        _logger.add(sys.stderr, level="WARNING", format="<level>{message}</level>")
    except Exception as exc:
        logger.debug(f"重配置 loguru 失败，沿用默认日志配置：{exc}")

    async def _run() -> None:
        url = f"ws://{host}:{port}/ws/attach?token={token}"
        transport = WsAttachTransport(
            url,
            handshake_frame={"type": "attach", "workspace": workspace},
            # 重连后重拉握手快照：on_reconnect 由 transport 在重连成功后同步调用，
            # 服务端 attached 帧会随重发握手（_open_once）重推，RootShim 据此
            # 重建 usage/continuation/会话镜像，消除断连窗口的镜像漂移。
            on_reconnect=lambda: None,
        )
        shim = RootShim(transport)
        try:
            await transport.connect()
        except Exception as exc:
            console.print(f"[red]无法连接内核（{host}:{port}）：{exc}[/red]")
            with contextlib.suppress(Exception):
                await transport.close()
            return
        # 等握手快照：attached 帧到达后 shim.ready 置位（RootShim 自己路由）。
        # 服务端拒绝（鉴权失败/未知空间）会立刻断开连接，
        # 不再干等超时——connection_state 帧已让 transport.connected 翻 False。
        for _ in range(200):  # 10s 封顶
            if shim.ready or not transport.connected:
                break
            await asyncio.sleep(0.05)
        if not shim.ready:
            console.print("[red]接入失败：未收到内核握手快照（鉴权失败 / 内核未就绪 / 版本过旧）。[/red]")
            await transport.close()
            raise SystemExit(1)
        await run_attached_chat_session(shim, workspace=workspace_dir, console=console)

    try:
        asyncio.run(_run())
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return 0
    return 0


_instance_lock_home: Path | None = None


def _reconcile_instance_lock_with_config() -> None:
    """config 加载后按最终 coara_home 口径对账（仅与早期解析不同时补锁）。"""
    from src.core.instance_lock import InstanceLockError, acquire_instance_lock, release_instance_lock

    final_home = config_manager.config.coara_home
    if final_home is None or _instance_lock_home is None:
        return
    if final_home.resolve() == _instance_lock_home.resolve():
        return
    try:
        acquire_instance_lock(final_home)
    except InstanceLockError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    # 早期锁的目录并非实际数据目录，撤掉换最终目录的锁
    release_instance_lock(_instance_lock_home)


def _resolve_matrix_config(ctx: click.Context, enable_matrix: bool) -> tuple[bool, dict[str, Any] | None]:
    """Check Matrix configuration; degrade gracefully if incomplete.

    Returns (actual_enabled, config_dict). When Matrix is requested but
    homeserver/user/password are missing, returns (False, None) and prints
    a warning so the user knows Matrix was skipped.

    GoMatrix 由 coara 托管（启动 coara 即自动拉起）。PC coara 默认连接
    ``http://127.0.0.1:8008``；手机 App 扫 coara WebUI「手机」页的配对码。
    """
    if not enable_matrix:
        return False, None

    config = ctx.obj.get("config")
    matrix_cfg = config.matrix if config else None

    # 先补全 gomatrix 凭证（生成/写 system/.env + os.environ），确保 homeserver/user/password 完整；
    # 否则全新安装时凭证尚未生成，会被误判「配置不完整」而降级（凭证补全原在 GoMatrixHost 才触发，顺序靠后）
    try:
        from src.core.coara_home import resolve_coara_home
        from src.matrix_host.credentials import ensure_matrix_bot_credentials

        coara_home = resolve_coara_home(ctx.obj["workspace"], getattr(config, "coara_home", None) if config else None)
        port = int(getattr(matrix_cfg, "port", 8008) or 8008) if matrix_cfg else 8008
        ensure_matrix_bot_credentials(coara_home, port=port)
    except Exception as exc:
        # 补全失败不阻塞启动；下方配置不完整时已有黄色降级提示对用户可见
        logger.debug(f"补全 matrix bot 凭证失败：{exc}")

    settings = build_matrix_settings(matrix_cfg)
    if matrix_config_incomplete(settings):
        console.print(
            "[yellow][Matrix] 配置不完整，已降级为不启动 Matrix。"
            "请设置 COARA_MATRIX_HOMESERVER / COARA_MATRIX_USER / COARA_MATRIX_PASSWORD "
            "或 config.yaml matrix 段[/yellow]"
        )
        return False, None

    return True, {
        "homeserver": settings["homeserver"],
        "user": settings["user"],
        "password": settings["password"],
        "notify_room_id": settings["notify_room_id"],
    }


async def _connect_matrix_if_ready(_matrix_config: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Connect to the configured homeserver; fall back to local GoMatrix when needed."""
    config = config_manager.config
    matrix_cfg = config.matrix if config else None
    coara_home = config.coara_home if config else None
    ok, resolved, warnings = await prepare_matrix_connection(matrix_cfg, coara_home=coara_home)
    for line in warnings:
        console.print(f"[yellow]{line}[/yellow]")
    if not ok:
        return False, None
    return True, resolved


async def _run_frontends(ctx: click.Context) -> None:
    """无头内核宿主：Web/Matrix（daemon / tray）。裸 ``coara`` 走 attach，不进此路径。"""
    workspace = ctx.obj["workspace"]
    # 10054 过滤器装进 asyncio.run 创建的这个运行循环（装在入口处的旧循环上无效）
    _setup_asyncio_exception_handler()
    # 产品化：无 -v/--verbose。console 恒 WARNING，开发者后门 COARA_DEBUG=1。
    debug = os.environ.get("COARA_DEBUG") == "1"

    # 正式 setup_logger 前先压掉 loguru 默认 handler（DEBUG 直出 stderr），
    # 避免 config 加载时的初始化日志在启动期刷屏（日志仍会进文件）。
    from src.core.logger import logger as _logger

    _logger.remove()
    _logger.add(sys.stderr, level="WARNING", format="<level>{message}</level>", colorize=True)

    from src.core.coara_home import ensure_system_config_templates, resolve_coara_home

    coara_home = resolve_coara_home(workspace, configured_home=os.environ.get("COARA_HOME"))
    seeded = ensure_system_config_templates(coara_home)
    if seeded:
        console.print("[dim]已补全缺失配置：" + "、".join(p.name for p in seeded) + "[/dim]")

    await config_manager.load()
    ctx.obj["config"] = config_manager.config
    _reconcile_instance_lock_with_config()

    # coara_home 切换的数据迁移：config 加载后、各模块初始化前（数据文件尚未打开
    # 的安全窗口）检测待迁移标记，确认后全套复制旧 home → 新 home。
    from src.cli.home_migration_prompt import maybe_run_home_migration

    await maybe_run_home_migration(console, config_manager.config, enable_cli=False)

    # 配置加载容错上报：YAML 解析失败/字段值非法回退默认时 启动即红字提示
    if config_manager.load_errors:
        for err in config_manager.load_errors:
            console.print(f"[red][配置] {err}[/red]")
        console.print("[yellow]以上配置项已按默认值运行，请检查配置文件后修正。[/yellow]")

    # coara_home 可写性自检：磁盘满/只读时提前发现，而非运行时持久化全失败
    home = config_manager.config.coara_home
    if home is not None:
        try:
            probe = Path(home) / ".coara_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            console.print(f"[red][严重] 数据目录 {home} 不可写：{exc}[/red]")
            console.print("[red]coara 无法持久化任何数据（会话/配置/记录），请检查磁盘空间与目录权限。[/red]")
            raise SystemExit(1) from None

    # 账户门禁（可选能力）：装了账户实现才有这回事，开源发行版直接跳过。
    # 回合入口在 CoaraBase.process_message 统一拦截；CLI 交互模式启动时也校验。
    try:
        from src.ext import gate_is_trial, startup_gate

        # 登录/付费是内核逻辑，回合入口统一拦截（到期任意端对话给登录提醒），
        # 启动期不卡——仅 TRIAL 状态提示一句，不强制引导登录。
        gate = await startup_gate(coara_home)
        if gate is not None and gate_is_trial(gate):
            # 文案由实现包给出：内核里不留任何商业化措辞
            console.print(f"[dim]{getattr(gate, 'reason', '') or '未登录'}[/dim]")
    except Exception as exc:
        # 可用性优先：门禁评估异常（多为网络/许可服务不可达）不阻塞启动，
        # 由回合入口按本地凭证与试用状态放行；仅记录日志。
        from loguru import logger

        logger.warning(f"账户门禁评估失败（启动放行，回合入口再判定）: {exc}")

    # 无任何可用 key 时进入首次启动向导；已有 key 时内部先对齐默认 provider 再返回
    from src.cli.first_run_setup import maybe_run_first_run_setup

    await maybe_run_first_run_setup(console)

    # Pre-check Matrix (degrade gracefully if unconfigured).
    actual_matrix, matrix_config = _resolve_matrix_config(ctx, True)
    ctx.obj["matrix_enabled"] = actual_matrix

    from src.cli.runtime_bootstrap import bootstrap_runtime
    from src.ui.trace_recording import (
        install_multi_workspace_trace_persistence,
        make_workspace_switched_handler,
    )

    setup_logger(
        log_level="DEBUG" if debug else config_manager.config.log_level,
        enable_console=True,
        rich_console=console,
        console_level="DEBUG" if debug else "WARNING",
        workspace_dir=workspace,
        coara_home=config_manager.config.coara_home,
        clear_on_start=True,
    )

    root, provider_name, model_name = await bootstrap_runtime(ctx)
    config_coara_home = getattr(ctx.obj.get("config"), "coara_home", None)

    _trace_holder: list[Any] = []
    install_multi_workspace_trace_persistence(
        root,
        workspace,
        coara_home=config_coara_home,
        trace_holder=_trace_holder,
    )

    root.event_bus.subscribe(
        make_workspace_switched_handler(root, config_coara_home, _trace_holder),
        topic="workspace_switched",
    )

    tasks: list[asyncio.Task] = []

    # Web frontend (shares root).
    from src.ui.web_server import run_web_server

    web_task = asyncio.create_task(
        run_web_server(
            workspace=workspace,
            port=int(os.environ.get("COARA_WEB_PORT", "8080")),
            provider=provider_name,
            model=model_name,
            workspace_alias=ctx.obj.get("workspace_alias"),
            root=root,
            auto_open=False,
        )
    )
    tasks.append(web_task)

    # Matrix frontend — connect to already-running GoMatrix server
    if actual_matrix and matrix_config:
        actual_matrix, matrix_config = await _connect_matrix_if_ready(matrix_config)
        if not actual_matrix or matrix_config is None:
            pass
        else:
            from src.cli.matrix_runner import run_matrix_client

            matrix_task = asyncio.create_task(
                run_matrix_client(root, matrix_config, coara_home=config_coara_home)
            )
            tasks.append(matrix_task)
            console.print(f"[cyan][Matrix] 正在连接 {matrix_config['homeserver']}…[/cyan]")

    alias = root.workspace_manager.active_name if root.workspace_manager else "?"
    console.print(f"[green]已就绪[/green] [bold]{root.identity.name}[/bold]（{alias}）")
    console.print("[dim]按 Ctrl+C 停止。[/dim]")

    # 后台自动更新：查新版 → 下载 + SHA256 校验 → 落 pending 标记；实际装包
    # 在下次启动时本地进行（运行中绝不替换文件）。任何失败静默。
    from src.cli.auto_update import schedule_background_update

    def _on_update_staged(pending: dict) -> None:
        console.print(f"[cyan]发现新版本 v{pending.get('version')}，已下载就绪，下次启动时自动应用。[/cyan]")

    schedule_background_update(on_staged=_on_update_staged)

    from src.cli.shutdown_signals import (
        install_sigterm_handler,
        install_windows_console_close_handler,
        make_minimal_sync_cleanup,
    )

    def _close_trace_stores_sync() -> None:
        """Close all per-workspace stores via the multi-workspace router."""
        router = _trace_holder[2] if len(_trace_holder) >= 3 else getattr(root, "trace_persistence", None)
        if router is not None:
            router.close()
        elif len(_trace_holder) >= 2:
            cur_store, cur_sub = _trace_holder[0], _trace_holder[1]
            cur_store.flush(timeout=1.0)
            cur_store.close()
            cur_sub.unsubscribe()

    stop_event = asyncio.Event()
    _loop = asyncio.get_running_loop()

    def _request_stop_from_signal() -> None:
        with contextlib.suppress(Exception):
            _loop.call_soon_threadsafe(stop_event.set)

    # Windows: closing the console window / taskkill does not raise SIGINT —
    # do a minimal synchronous flush from the OS handler thread (~5s budget).
    _uninstall_console_close = install_windows_console_close_handler(
        make_minimal_sync_cleanup(root, close_trace=_close_trace_stores_sync)
    )

    try:
        with install_sigterm_handler(_request_stop_from_signal):
            await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if _uninstall_console_close is not None:
            _uninstall_console_close()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            _close_trace_stores_sync()
        with contextlib.suppress(Exception):
            await root.shutdown()


def _coara_version() -> str:
    with contextlib.suppress(Exception):
        from importlib.metadata import version

        return version("coara")
    return "0.0.0"


@click.group(invoke_without_command=True)
@click.version_option(version=_coara_version(), prog_name="coara")
@click.option("--workspace", type=click.Path(path_type=Path), help="Workspace directory")
@click.option(
    "--workspace-alias",
    type=str,
    help="Registered workspace alias to activate (e.g. my-app)",
)
@click.option("--provider", "-p", type=str, help="LLM provider name")
@click.option("--model", "-m", type=str, help="Model name")
@click.pass_context
def cli(
    ctx: click.Context,
    workspace: Path | None,
    workspace_alias: str | None,
    provider: str | None,
    model: str | None,
) -> None:
    """coara - an AI assistant runtime that can create and organize sub-agents."""
    _setup_asyncio_exception_handler()
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace or Path.cwd()
    ctx.obj["workspace_alias"] = workspace_alias
    ctx.obj["provider"] = provider
    ctx.obj["model"] = model

    if ctx.invoked_subcommand is None:
        # 统一启动：内核常驻；本进程只是完整样子的 CLI 客户端（attach）
        # attach 客户端是薄壳：不写文件日志，console 只显示 WARNING 以上
        # （COARA_DEBUG=1 显示全量），避免 config 加载等 DEBUG 日志刷屏。
        debug = os.environ.get("COARA_DEBUG") == "1"
        try:
            from src.core.logger import setup_logger

            setup_logger(
                log_level="DEBUG" if debug else "INFO",
                enable_file_logging=False,
                enable_console=True,
                console_level="DEBUG" if debug else "WARNING",
            )
        except Exception:
            # setup_logger 本身失败说明 logger 多半已坏，记日志也多半丢失，保持静默
            pass
        _set_console_title("coara")
        _ensure_kernel_and_attach(ctx.obj["workspace"])


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """显示运行环境状态。"""

    async def run_status() -> None:
        await config_manager.load()
        from src.cli.first_run_setup import providers_with_usable_keys

        console.print("[bold cyan]考拉状态[/bold cyan]")
        console.print(f"工作目录：{ctx.obj['workspace']}")
        providers = ", ".join(p.name for p in providers_with_usable_keys()) or "（未添加模型，用 /model --add 添加）"
        console.print(f"已配置供应商：{providers}")
        if config_manager.config.default_provider:
            console.print(f"默认配置档：{config_manager.config.default_profile or '无'}")
            console.print(f"默认供应商：{config_manager.config.default_provider or '无'}")
            console.print(f"默认模型：{config_manager.config.default_model or '无'}")
        else:
            console.print("默认供应商：（未设置，添加第一个模型后自动设为默认）")

    asyncio.run(run_status())


@cli.command("daemon")
@click.pass_context
def daemon(ctx: click.Context) -> None:
    """无头内核常驻服务：启动内核并托管 Web/Matrix 前端，无 CLI 交互界面。

    阶段 2：独立内核进程的入口；主 CLI 作为 client 经本地 WS 接入（后置）。
    """
    from src.coara.workspace_runtime import load_active_runtime
    from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home

    home = resolve_bootstrap_coara_home() or resolve_coara_home(ctx.obj["workspace"])
    from src.runtime.restart import has_pending_restart

    running = load_active_runtime(home)
    # 重启交棒中放行：旧内核已让位、正在退场，这里再拦一次，交棒就永远走不完
    # （/restart 会以「新实例启动即退出 code=0」回退）。
    if running is not None and not has_pending_restart(home):
        console.print(f"[red]内核已在运行（PID {running.pid}，空间 {running.workspace_name}）。[/red]")
        raise SystemExit(0)
    _acquire_instance_lock_or_exit(ctx.obj["workspace"])
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_frontends(ctx))


@cli.command("tray")
@click.pass_context
def tray(ctx: click.Context) -> None:
    """系统托盘常驻入口：起内核 daemon（托管 Web/Matrix）+ 托盘图标。

    阶段 4：点击托盘图标或 ``coara tray`` 命令同一起点——无内核先起内核，
    已有内核则提示复用。右键菜单：打开 Web / 手机连接 / 退出。
    无 pystray 时退化为纯 daemon（提示安装 desktop extra）。
    """
    from src.coara.workspace_runtime import load_active_runtime
    from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home

    home = resolve_bootstrap_coara_home() or resolve_coara_home(ctx.obj["workspace"])
    from src.runtime.restart import has_pending_restart

    running = load_active_runtime(home)
    # 重启交棒中放行：旧内核已让位、正在退场，这里再拦一次，交棒就永远走不完
    # （/restart 会以「新实例启动即退出 code=0」回退）。
    if running is not None and not has_pending_restart(home):
        console.print(f"[red]内核已在运行（PID {running.pid}，空间 {running.workspace_name}）。[/red]")
        raise SystemExit(0)
    _acquire_instance_lock_or_exit(ctx.obj["workspace"])

    from src.cli.tray import run_tray

    loop_holder: dict = {}

    def _open_web() -> None:
        # 带 token 打开；已有活跃标签则只唤起（不开新标签再被 SingleTabGuard 关掉）。
        from src.ui.dashboard_tokens import load_or_create_dashboard_token
        from src.ui.web_server import open_or_focus_web_ui

        try:
            home = None
            try:
                from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home

                home = resolve_bootstrap_coara_home() or resolve_coara_home(ctx.obj["workspace"])
            except Exception:
                home = None
            token = load_or_create_dashboard_token(ctx.obj["workspace"], home) if home else ""
        except Exception:
            token = ""
        port = int(os.environ.get("COARA_WEB_PORT", "8080"))
        # 未配置任何可用模型时直落配置页：新用户装完第一屏就是填 key 的地方，
        # 配好即对话（配置概况注入的 config-assistant 浮窗同页可继续加 provider）。
        path = "/"
        try:
            from src.core.config import config_manager as _cfg_mgr
            from src.llm.model_catalog import _enabled_provider_names

            if not _enabled_provider_names(_cfg_mgr):
                path = "/config"
        except Exception:
            path = "/"
        # 带上目标路径：已有标签时内核推 focus_window(path)，端上定向导航过去
        # （SPA 内跳转，不开新标签、不刷新）。不带 path 时前端只剩 window.focus()，
        # 而浏览器对无用户手势的 focus() 基本忽略 → 体感「点了没反应」。
        open_or_focus_web_ui(host="127.0.0.1", port=port, token=token, path=path)

    def _open_mobile() -> None:
        # 手机连接二维码做到托盘：取 gomatrix qr.png 存临时文件，用系统默认程序
        # 打开显示（手机扫屏幕上的图即连）。二维码从 webUI 拿下，统一收到托盘。
        import tempfile

        from src.coara.commands.qrcode import _dashboard_status, _fetch_bytes, _gomatrix_port

        port = _gomatrix_port()
        status = _dashboard_status()
        if status is None or not (status.get("tunnel_ready") and status.get("tunnel_url")):
            # 隧道未就绪/未启用：退回打开 Web 手机页（含局域网手动填写指引）。
            _open_web()
            return
        png = _fetch_bytes(f"http://127.0.0.1:{port}/dashboard/qr.png")
        if not png:
            _open_web()
            return
        try:
            tmp = Path(tempfile.gettempdir()) / "coara-mobile-qr.png"
            tmp.write_bytes(png)
            if sys.platform == "win32":
                os.startfile(str(tmp))  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(tmp)])
        except OSError:
            _open_web()

    def _quit_kernel() -> None:
        loop = loop_holder.get("loop")
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_raise_keyboard_interrupt(loop))

    def _raise_keyboard_interrupt(loop):  # type: ignore[no-untyped-def]
        def _stop() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()

        return _stop

    tray_launcher = run_tray(
        open_web=_open_web,
        open_mobile=_open_mobile,
        quit_kernel=_quit_kernel,
    )
    if tray_launcher is None:
        console.print("[yellow]未检测到 pystray，托盘不可用；按纯 daemon 运行（pip install pystray 启用）。[/yellow]")

    async def _auto_open_web_when_ready() -> None:
        """双击桌面图标：内核起好后默认打开一次 Web（用户要的桌面体验）。"""
        from src.ui.web_server import _wait_until_ready

        if await _wait_until_ready("localhost", 8080, timeout_s=15.0):
            _open_web()

    with contextlib.suppress(KeyboardInterrupt):
        loop = asyncio.new_event_loop()
        loop_holder["loop"] = loop
        try:
            asyncio.set_event_loop(loop)
            loop.create_task(_auto_open_web_when_ready())
            loop.run_until_complete(_run_frontends(ctx))
        finally:
            if tray_launcher is not None:
                tray_launcher.stop()
            loop.close()


@cli.command()
def providers() -> None:
    """List available LLM providers."""

    async def run_providers() -> None:
        from src.llm.registry import initialize_providers

        await config_manager.load()
        await initialize_providers(config_manager)
        console.print("[bold cyan]可用供应商[/bold cyan]")
        for name in config_manager.list_providers():
            provider = config_manager.get_provider(name)
            driver = provider.driver or "（启动时推断）"
            default_model = provider.default_model or provider.models.get("default", "无")
            console.print(
                f"  [green]{name}[/green]：驱动 {driver} · 地址 {provider.base_url} · 默认模型 {default_model}"
            )

    asyncio.run(run_providers())


@cli.command("llm-profiles")
def llm_profiles_cmd() -> None:
    """List resolved LLM profiles (provider + model per consumer)."""

    async def run_profiles() -> None:
        from src.llm.registry import initialize_providers
        from src.llm.service import llm_service

        await config_manager.load()
        await initialize_providers(config_manager)
        console.print("[bold cyan]LLM 配置档[/bold cyan]")
        default_profile = config_manager.config.default_profile
        console.print(f"默认配置档：{default_profile}")
        for profile_name in llm_service.list_profiles():
            resolved = llm_service.resolve(profile_name)
            console.print(
                f"  [green]{profile_name}[/green]："
                f"供应商 {resolved.provider_name} · 模型 {resolved.model} · "
                f"max_tokens={resolved.max_tokens} · temperature={resolved.temperature}"
            )

    asyncio.run(run_profiles())


@cli.group(name="config")
def config_group() -> None:
    """查看当前生效配置（脱敏后）。"""


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """显示当前生效配置的来源链与关键值（密钥一律脱敏为 ***）。"""

    async def run_show() -> None:
        from src.core.coara_home import resolve_coara_home
        from src.core.config import _iter_config_yaml_paths, _iter_env_file_paths, mask_secrets

        await config_manager.load()
        cfg = config_manager.config

        console.print("[bold cyan]生效配置[/bold cyan]")
        home = resolve_coara_home(ctx.obj["workspace"], cfg.coara_home)
        console.print(f"coara_home：{home}")

        env_files = [str(p) for p in _iter_env_file_paths(config_manager._raw_config) if p.is_file()]
        yaml_files = [str(p) for p in _iter_config_yaml_paths(config_manager._raw_config) if p.is_file()]
        if env_files:
            console.print("env 文件（按加载顺序）：")
            for p in env_files:
                console.print(f"  {p}")
        if yaml_files:
            console.print("YAML 文件（按优先级由低到高）：")
            for p in yaml_files:
                console.print(f"  {p}")

        if cfg.default_provider:
            console.print(f"默认供应商：{cfg.default_provider} · 默认模型：{cfg.default_model or '无'}")
        else:
            console.print("默认供应商：（未设置）")
        vault_on = "开" if cfg.vault_enabled else "关"
        skills_on = "开" if cfg.skills_enabled else "关"
        console.print(f"日志级别：{cfg.log_level} · vault：{vault_on} · 技能：{skills_on}")

        if config_manager._load_errors:
            console.print(f"[yellow]加载告警 {len(config_manager._load_errors)} 条：[/yellow]")
            for err in config_manager._load_errors:
                console.print(f"  {err}")

        import json as _json

        masked = mask_secrets(config_manager._raw_config)
        console.print("[dim]合并后原始配置（脱敏）：[/dim]")
        console.print(_json.dumps(masked, ensure_ascii=False, indent=2, default=str))

    asyncio.run(run_show())


def _vault_service_for_cli(ctx: click.Context):
    from src.core.coara_home import resolve_coara_home
    from src.core.config import config_manager
    from src.vault import VaultService

    async def build():
        await config_manager.load()
        coara_home = resolve_coara_home(ctx.obj["workspace"], config_manager.config.coara_home)
        service = VaultService(coara_home)
        await service.initialize()
        service.try_unlock_from_env()
        return service

    return build


@cli.command()
@click.argument("query", required=False)
@click.option("--no-prompt", is_flag=True, help="Do not prompt; require COARA_VAULT_PASSWORD")
@click.pass_context
def search(ctx: click.Context, query: str | None, no_prompt: bool) -> None:
    """Search filenames under the vault open/ tree (prompts to unlock in this process)."""
    if not query:
        query = Prompt.ask("[bold blue]搜索关键词[/bold blue]")

    async def run_search() -> None:
        service = await _vault_service_for_cli(ctx)()
        if not service.is_initialized():
            console.print("[yellow]保险柜未初始化。请先运行 coara vault init。[/yellow]")
            await service.shutdown()
            return
        if not _ensure_vault_unlocked(service, no_prompt=no_prompt):
            await service.shutdown()
            raise click.UsageError("保险柜未解锁")
        q = (query or "").casefold()
        root = service.open_dir
        hits = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and q in p.as_posix().casefold()]
        if not hits:
            console.print("[yellow]没有匹配结果。[/yellow]")
            await service.shutdown()
            return
        console.print(f"[bold cyan]保险柜搜索：「{query}」[/bold cyan]")
        for index, rel in enumerate(hits, start=1):
            console.print(f"{index}. {rel}")
        await service.shutdown()

    asyncio.run(run_search())


@cli.group(name="ws")
@click.pass_context
def ws_group(ctx: click.Context) -> None:
    """工作空间登记：list / add / remove / rename / default。"""


@ws_group.command(name="list")
@click.pass_context
def workspace_list(ctx: click.Context) -> None:
    """List registered workspaces."""

    async def run_list() -> None:
        from src.cli.workspace_cmds import _open_cli_registry, print_workspace_list

        registry = _open_cli_registry(ctx.obj["workspace"])
        print_workspace_list(ctx.obj["workspace"])
        active_name = registry.document.default_workspace
        if active_name:
            active_entry = registry.get_by_id(active_name)
            if active_entry:
                active_name = active_entry.name
        console.print(f"[cyan]默认[/cyan]：{active_name or '（无）'}")

    asyncio.run(run_list())


@ws_group.command(name="add")
@click.argument("path", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--name", type=str, help="Workspace name")
@click.option("--summary", type=str, help="One-line summary for ws(action=list) and CLI list")
@click.pass_context
def workspace_add(
    ctx: click.Context,
    path: Path,
    name: str | None,
    summary: str | None,
) -> None:
    """Register a workspace directory in the registry."""
    from src.cli.workspace_cmds import register_workspace

    register_workspace(ctx.obj["workspace"], path, name=name, summary=summary)


@ws_group.command(name="remove")
@click.argument("name")
@click.option(
    "--delete-disk",
    is_flag=True,
    help="同时删除工作空间磁盘目录（不可逆；默认只取消登记）",
)
@click.option(
    "--yes",
    is_flag=True,
    help="跳过 --delete-disk 的交互确认",
)
@click.pass_context
def workspace_remove(ctx: click.Context, name: str, delete_disk: bool, yes: bool) -> None:
    """从登记册移除工作空间；可选同时删除磁盘目录。"""
    from src.cli.workspace_cmds import remove_workspace

    remove_workspace(ctx.obj["workspace"], name, delete_disk=delete_disk, assume_yes=yes)


@ws_group.command(name="rename")
@click.argument("name")
@click.argument("new_name")
@click.pass_context
def workspace_rename(ctx: click.Context, name: str, new_name: str) -> None:
    """重命名已登记工作空间（路径与磁盘目录不动）。"""
    from src.cli.workspace_cmds import rename_workspace

    rename_workspace(ctx.obj["workspace"], name, new_name)


@ws_group.command(name="default")
@click.argument("name")
@click.pass_context
def workspace_default(ctx: click.Context, name: str) -> None:
    """设 NAME 为启动默认工作空间（不影响正在运行的 coara）。

    本子命令在独立进程里只改登记表默认项后退出。要切换正在运行的会话，
    用会话内的 ``/ws switch`` 或 ``ws(action=switch)``。
    """
    from src.cli.workspace_cmds import _open_cli_registry

    async def run_default() -> None:
        registry = _open_cli_registry(ctx.obj["workspace"])
        clean = name.strip()
        entry = registry.resolve_name_or_id(clean)
        if entry is None:
            console.print(f"[red]找不到工作空间「{name}」[/red]")
            return
        registry.set_default(entry.id)
        console.print(f"[green]已设为启动默认[/green] {entry.name}")

    asyncio.run(run_default())


@cli.command("errors")
@click.option("--days", type=int, default=7, show_default=True, help="Rolling window in days (0 = all)")
@click.option("--kind", "kind", default=None, help="Filter by error_kind")
@click.option("--tool", "tool_name", default=None, help="Filter by tool name")
@click.option("--session", "session_id", default=None, help="Filter by session id")
@click.option("--limit", type=int, default=20, show_default=True, help="Recent entries shown")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON")
@click.pass_context
def errors_cmd(
    ctx: click.Context,
    days: int,
    kind: str | None,
    tool_name: str | None,
    session_id: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """汇总当前工作空间的错误日志（errors.jsonl）。"""
    import json

    from src.core.config import config_manager
    from src.core.error_log_query import (
        format_error_summary_text,
        resolve_error_log_paths,
        summarize_errors,
        summary_to_dict,
    )

    try:
        coara_home = config_manager.config.coara_home
    except Exception:
        coara_home = None

    workspace = ctx.obj["workspace"]
    paths = resolve_error_log_paths(workspace, coara_home=coara_home)
    summary = summarize_errors(
        workspace_dir=workspace,
        coara_home=coara_home,
        days=days,
        kind=kind,
        tool=tool_name,
        session=session_id,
        limit=limit,
    )
    if as_json:
        console.print(json.dumps(summary_to_dict(summary), ensure_ascii=False, indent=2))
        return
    if not paths:
        console.print("还没有错误日志文件。")
        return
    console.print(format_error_summary_text(summary))
    console.print("")
    for path in paths:
        console.print(f"  {path}")


@cli.group(name="usage")
@click.pass_context
def usage_group(ctx: click.Context) -> None:
    """Inspect coara usage accounting (offline JSONL aggregation)."""


@usage_group.command(name="summary")
@click.option("--days", type=int, default=7, show_default=True, help="Rolling window in days")
@click.option("--path", "events_path", type=click.Path(path_type=Path), default=None, help="Override events.jsonl path")
@click.pass_context
def usage_summary(ctx: click.Context, days: int, events_path: Path | None) -> None:
    """Summarize LLM/tool usage for the current workspace."""
    from src.runtime.usage_query import format_summary_text, resolve_default_usage_path, summarize_usage

    path = events_path or resolve_default_usage_path(ctx.obj["workspace"])
    summary = summarize_usage(path, days=days)
    console.print(format_summary_text(summary))


@usage_group.command(name="session")
@click.argument("session_id")
@click.option("--path", "events_path", type=click.Path(path_type=Path), default=None, help="Override events.jsonl path")
@click.pass_context
def usage_session(ctx: click.Context, session_id: str, events_path: Path | None) -> None:
    """Show per-session usage breakdown."""
    import json

    from src.runtime.usage_query import resolve_default_usage_path, summarize_session

    path = events_path or resolve_default_usage_path(ctx.obj["workspace"])
    data = summarize_session(path, session_id)
    console.print(json.dumps(data, ensure_ascii=False, indent=2))


@cli.group(name="examples")
@click.pass_context
def examples_group(ctx: click.Context) -> None:
    """Install bundled example workspace configs into coara Home."""


@examples_group.command(name="install")
@click.argument("name")
@click.option("--coara-home", type=click.Path(path_type=Path), help="Target coara Home directory")
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    help="Workspace path to register (stocks-watch only)",
)
@click.option("--dry-run", is_flag=True, help="Show actions without writing files")
@click.pass_context
def examples_install(
    ctx: click.Context,
    name: str,
    coara_home: Path | None,
    workspace: Path | None,
    dry_run: bool,
) -> None:
    """Install an example pack (currently: stocks-watch)."""
    if name != "stocks-watch":
        raise click.ClickException(f"Unknown example pack '{name}'. Available: stocks-watch")

    from src.examples.install_stocks_watch import format_install_next_steps, install_stocks_watch

    try:
        result = install_stocks_watch(
            coara_home=coara_home,
            workspace=workspace,
            dry_run=dry_run,
            cwd=ctx.obj["workspace"],
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    prefix = "would write" if dry_run else "installed"
    console.print(f"[green]{prefix}[/green] stocks-watch → {result.coara_home}")
    for path in result.copied:
        console.print(f"  [dim]{'→' if dry_run else '✓'}[/dim] {path}")
    if result.registry_updated:
        console.print("  [dim]registry[/dim] @stocks-watch")
    console.print(f"\n[dim]{format_install_next_steps()}[/dim]")


# ------------------------------------------------------------------
# Vault CLI group: coara vault init/status/unlock/lock/list/read/write/import/passwd
#
# - Frontends (CLI/Web/Matrix) unlock via password side-channel.
# - Password SETUP / CHANGE are PC CLI only (init / passwd).
# - Offline vault CLI is per-process (exit → seal+lock).
# - Disk: sealed/*.cvault + open/ while unlocked; meta salt+verifier only.
# ------------------------------------------------------------------


@cli.group(name="vault")
@click.pass_context
def vault_group(ctx: click.Context) -> None:
    """Manage encrypted vault / 保险柜 (init/unlock/list/read/write/import/passwd).

    Offline CLI is per-process: unlock lasts only until the command exits
    (shutdown seals+locks). Use list/read/write/import which prompt for the
    password in the same process, or set COARA_VAULT_PASSWORD for non-interactive use.
    """


def _vault_service_for_group(ctx: click.Context):
    """Build a VaultService for the vault CLI group (mirrors _vault_service_for_cli)."""
    from src.core.coara_home import resolve_coara_home
    from src.core.config import config_manager
    from src.vault import VaultService

    async def build():
        await config_manager.load()
        coara_home = resolve_coara_home(ctx.obj["workspace"], config_manager.config.coara_home)
        service = VaultService(coara_home)
        await service.initialize()
        service.try_unlock_from_env()
        return service

    return build


def _prompt_password(confirm: bool = True, label: str = "主密码") -> str:
    """Prompt for a vault password without echoing. Confirms if asked."""
    import getpass

    pw = getpass.getpass(f"{label}: ")
    if not pw:
        raise click.UsageError("密码不能为空")
    if confirm:
        pw2 = getpass.getpass(f"再次输入{label}: ")
        if pw != pw2:
            raise click.UsageError("两次输入不一致")
    return pw


def _ensure_vault_unlocked(service, *, no_prompt: bool) -> bool:
    """Ensure the vault service is unlocked.

    If already unlocked, returns True. If locked and ``no_prompt`` is False,
    interactively prompts for the password. If locked and ``no_prompt`` is
    True (CI / scripted use), prints an error and returns False so the
    caller can exit cleanly without hanging on stdin.
    """
    if service.is_unlocked():
        return True
    if no_prompt:
        console.print("[red]保险柜已锁定，且 --no-prompt 已设置，无法交互式输入密码。[/red]")
        console.print("[dim]请设置 COARA_VAULT_PASSWORD，或去掉 --no-prompt。[/dim]")
        return False
    password = _prompt_password(confirm=False, label="主密码（解锁）")
    from src.vault.errors import VaultAuthError, VaultError

    try:
        service.unlock(password, persistent=False)
    except VaultAuthError:
        console.print("[red]密码错误。[/red]")
        return False
    except VaultError as exc:
        console.print(f"[red]解锁失败: {exc}[/red]")
        return False
    return True


@vault_group.command(name="status")
@click.pass_context
def vault_status(ctx: click.Context) -> None:
    """Show vault initialization / lock state and entry count."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        status = service.status()
        init_text = "[green]已初始化[/green]" if status.initialized else "[yellow]未初始化[/yellow]"
        lock_text = "[red]已锁定[/red]" if status.locked else "[green]已解锁[/green]"
        console.print("[bold cyan]保险柜状态[/bold cyan]")
        console.print(f"  状态：{init_text} · {lock_text}")
        console.print(f"  已封存：{status.entries} 项")
        console.print(f"  位置：{status.vault_dir}")
        if not status.initialized:
            console.print("\n[dim]运行 coara vault init 创建保险柜。[/dim]")
        elif status.locked:
            console.print("[dim]离线读写：coara vault list|read|write|import；对话里让助手打开保险柜。[/dim]")
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="init")
@click.pass_context
def vault_init(ctx: click.Context) -> None:
    """Initialize a new vault with a master password (manual PC operation)."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        if service.is_initialized():
            console.print("[yellow]保险柜已存在。如需修改密码请用 `coara vault passwd`。[/yellow]")
            await service.shutdown()
            return
        password = _prompt_password(confirm=True, label="设置主密码（≥8 位，字母数字混合）")
        try:
            service.setup_or_unlock(password, persistent=False)
        except ValueError as exc:
            console.print(f"[red]初始化失败: {exc}[/red]")
            await service.shutdown()
            raise click.UsageError(str(exc)) from exc
        console.print("[green]保险柜已创建。[/green]")
        console.print(
            "[dim]离线 CLI 退出后会自动上锁。请用 `coara vault list|read|write|import` "
            "在同一命令进程内操作，或设置 COARA_VAULT_PASSWORD。[/dim]"
        )
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="unlock")
@click.pass_context
def vault_unlock(ctx: click.Context) -> None:
    """Verify master password (offline CLI does not keep an unlock across commands)."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        if not service.is_initialized():
            console.print("[yellow]保险柜尚未初始化。请先运行 `coara vault init`。[/yellow]")
            await service.shutdown()
            return
        password = _prompt_password(confirm=False, label="主密码")
        from src.vault.errors import VaultAuthError

        try:
            service.unlock(password, persistent=False)
        except VaultAuthError:
            console.print("[red]密码错误。[/red]")
            await service.shutdown()
            raise click.UsageError("密码错误") from None
        except Exception as exc:
            console.print(f"[red]解锁失败: {exc}[/red]")
            await service.shutdown()
            raise click.UsageError(str(exc)) from exc
        console.print("[green]密码正确。[/green]")
        console.print(
            "[dim]离线 CLI 退出后会重新上锁，不会跨命令保持解锁。"
            "读写请直接用 `coara vault list|read|write|import`（会提示输入密码），"
            "或设置 COARA_VAULT_PASSWORD；长会话请用 `coara` 内 `vault(action=open)`。[/dim]"
        )
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="lock")
@click.pass_context
def vault_lock(ctx: click.Context) -> None:
    """Lock the vault session (clears in-memory DEK)."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        if not service.is_initialized():
            console.print("[yellow]保险柜未初始化。[/yellow]")
            await service.shutdown()
            return
        service.lock()
        console.print("[green]保险柜已锁定。[/green]")
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="list")
@click.option("--no-prompt", is_flag=True, help="Do not prompt; require COARA_VAULT_PASSWORD")
@click.pass_context
def vault_list(ctx: click.Context, no_prompt: bool) -> None:
    """List files in the open/ working tree (prompts to unlock in this process)."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        if not service.is_initialized():
            console.print("[yellow]保险柜未初始化。[/yellow]")
            await service.shutdown()
            return
        if not _ensure_vault_unlocked(service, no_prompt=no_prompt):
            await service.shutdown()
            raise click.UsageError("保险柜未解锁")
        root = service.open_dir
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".coara_vault_session")
        if not files:
            console.print("[dim]工作目录为空[/dim]")
            await service.shutdown()
            return
        console.print("[bold cyan]保险柜内容[/bold cyan]")
        for path in files:
            rel = path.relative_to(root).as_posix()
            console.print(f"  {rel}")
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="read")
@click.argument("rel_path")
@click.option("--no-prompt", is_flag=True, help="Do not prompt; require COARA_VAULT_PASSWORD")
@click.pass_context
def vault_read(ctx: click.Context, rel_path: str, no_prompt: bool) -> None:
    """Read a text file from the open/ tree (prompts to unlock in this process)."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        if not _ensure_vault_unlocked(service, no_prompt=no_prompt):
            await service.shutdown()
            raise click.UsageError("保险柜未解锁")
        target = (service.open_dir / rel_path.replace("\\", "/")).resolve()
        try:
            target.relative_to(service.open_dir.resolve())
        except ValueError:
            console.print("[red]路径非法。[/red]")
            await service.shutdown()
            return
        if not target.is_file():
            console.print(f"[red]文件不存在: {rel_path}[/red]")
            await service.shutdown()
            return
        console.print(target.read_text(encoding="utf-8", errors="replace"))
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="write")
@click.option("--path", "rel_path", prompt="相对路径", help="相对 open/ 的路径，如 notes/a.txt")
@click.option("--body", help="文本正文（不填则交互输入）")
@click.option("--no-prompt", is_flag=True, help="Do not prompt; require COARA_VAULT_PASSWORD")
@click.pass_context
def vault_write(ctx: click.Context, rel_path: str, body: str | None, no_prompt: bool) -> None:
    """Write a text file into the open/ tree (prompts to unlock in this process)."""

    async def run() -> None:
        nonlocal body
        service = await _vault_service_for_group(ctx)()
        if not _ensure_vault_unlocked(service, no_prompt=no_prompt):
            await service.shutdown()
            raise click.UsageError("保险柜未解锁")
        if not body:
            console.print("[bold]输入正文（空行结束）:[/bold]")
            lines: list[str] = []
            try:
                while True:
                    line = input()
                    if line == "":
                        break
                    lines.append(line)
            except EOFError:
                pass
            body = "\n".join(lines)
        target = (service.open_dir / rel_path.replace("\\", "/")).resolve()
        try:
            target.relative_to(service.open_dir.resolve())
        except ValueError:
            console.print("[red]路径非法。[/red]")
            await service.shutdown()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body or "", encoding="utf-8")
        console.print(f"[green]已写入: {rel_path}[/green]")
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="import")
@click.argument("file_path", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--path", "rel_path", help="相对 open/ 的目标路径（默认用文件名）")
@click.option("--no-prompt", is_flag=True, help="Do not prompt; require COARA_VAULT_PASSWORD")
@click.pass_context
def vault_import(ctx: click.Context, file_path: Path, rel_path: str | None, no_prompt: bool) -> None:
    """Copy a file into the open/ tree (prompts to unlock in this process)."""

    async def run() -> None:
        import shutil

        service = await _vault_service_for_group(ctx)()
        if not _ensure_vault_unlocked(service, no_prompt=no_prompt):
            await service.shutdown()
            raise click.UsageError("保险柜未解锁")
        dest_rel = (rel_path or file_path.name).replace("\\", "/")
        target = (service.open_dir / dest_rel).resolve()
        try:
            target.relative_to(service.open_dir.resolve())
        except ValueError:
            console.print("[red]路径非法。[/red]")
            await service.shutdown()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)
        console.print(f"[green]已导入: {dest_rel}[/green]")
        await service.shutdown()

    asyncio.run(run())


@vault_group.command(name="passwd")
@click.pass_context
def vault_passwd(ctx: click.Context) -> None:
    """Change the vault master password (old → new verification)."""

    async def run() -> None:
        service = await _vault_service_for_group(ctx)()
        if not service.is_initialized():
            console.print("[yellow]保险柜未初始化。请先运行 coara vault init。[/yellow]")
            await service.shutdown()
            return
        old_password = _prompt_password(confirm=False, label="旧主密码")
        new_password = _prompt_password(confirm=True, label="新主密码（≥8 位，字母数字混合）")
        from src.vault.errors import VaultAuthError

        try:
            count = service.change_password(old_password=old_password, new_password=new_password)
        except VaultAuthError:
            console.print("[red]旧密码错误。[/red]")
            await service.shutdown()
            raise click.UsageError("旧密码错误") from None
        except ValueError as exc:
            console.print(f"[red]修改失败: {exc}[/red]")
            await service.shutdown()
            raise click.UsageError(str(exc)) from exc
        console.print(f"[green]主密码已修改，已重新加密 {count} 个文件。[/green]")
        console.print(f"[dim]工作目录: {service.open_dir.resolve()}[/dim]")
        await service.shutdown()

    asyncio.run(run())


@cli.command()
@click.argument("workspace_name")
@click.pass_context
def attach(ctx: click.Context, workspace_name: str) -> None:
    """外挂接入正在运行的 coara 主进程，对指定工作空间发起对话（纯客户端）。

    本命令是独立瘦客户端进程：不抢单实例锁、不起本地核心，只经
    WebSocket 连主进程的 /ws/attach。主进程未启动时会给出友好报错。
    """
    from src.cli.attach_client import load_attach_token, resolve_web_host_port, run_attach_command

    host, port = resolve_web_host_port()
    token = load_attach_token(ctx.obj["workspace"])
    code = run_attach_command(workspace_name, workspace_dir=ctx.obj["workspace"], host=host, port=port, token=token)
    if code:
        raise SystemExit(code)


def main() -> None:
    """Main entrypoint."""
    cli()


if __name__ == "__main__":
    main()
