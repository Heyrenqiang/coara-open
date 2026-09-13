"""系统托盘 launcher：常驻托盘图标，右键菜单拉起 web / 手机连接 / 退出内核。

定位：web UI 已承担桌面界面，托盘只做「常驻入口」——不开终端也能起内核、
拉起 web、退出内核。点击托盘图标或 ``coara tray`` 命令同一起点：
无内核先起内核 daemon（托管 web/matrix），已有内核则复用。

依赖：pystray（desktop extra，可选）。无 pystray 时 :func:`run_tray` 提示并退出。
pystray 消息循环跑在独立线程，内核 asyncio 事件循环跑在主线程，二者经
``threading`` + ``asyncio.run_coroutine_threadsafe`` 桥接。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any

from src.core.logger import logger


def _pystray_available() -> bool:
    try:
        import pystray  # noqa: F401

        return True
    except Exception:
        return False


def _build_icon_image() -> Any:
    """托盘图标：固定资产 src/cli/coara.ico（与桌面快捷方式同一 ico，随包发布）。"""
    from pathlib import Path

    from PIL import Image

    with Image.open(Path(__file__).resolve().parent / "coara.ico") as img:
        return img.copy()


class TrayLauncher:
    """托盘常驻入口：持内核回调，构建菜单。"""

    def __init__(
        self,
        *,
        open_web: Callable[[], None],
        open_mobile: Callable[[], None],
        quit_kernel: Callable[[], None],
    ) -> None:
        self._open_web = open_web
        self._open_mobile = open_mobile
        self._quit_kernel = quit_kernel
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    def _menu(self) -> Any:
        import pystray

        def on_quit(icon: Any, _item: Any) -> None:
            # 先卸图标再停内核：Windows 上不调 stop() 会留下幽灵托盘项
            # （鼠标划过才消失）；若先 cancel 事件循环，进程可能在 stop 前退出。
            with contextlib.suppress(Exception):
                icon.stop()
            self._quit_kernel()

        return pystray.Menu(
            pystray.MenuItem("打开 Web 界面", lambda: self._open_web(), default=True),
            pystray.MenuItem("手机连接（二维码）", lambda: self._open_mobile()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 coara", on_quit),
        )

    def run(self) -> None:
        """在当前线程跑 pystray 消息循环（阻塞，直到 stop）。"""
        import pystray

        self._icon = pystray.Icon("coara", _build_icon_image(), "coara 考拉", self._menu())
        try:
            self._icon.run()
        finally:
            self._icon = None

    def stop(self) -> None:
        """从任意线程请求卸掉托盘图标并结束消息循环。"""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.stop()
        except Exception as exc:
            logger.debug(f"tray icon stop failed: {exc}")


def run_tray(
    *,
    open_web: Callable[[], None],
    open_mobile: Callable[[], None],
    quit_kernel: Callable[[], None],
) -> TrayLauncher | None:
    """在独立线程启动托盘；无 pystray 时打印提示并返回 None。

    返回 :class:`TrayLauncher`（可 ``stop()`` 卸图标）；不可用返回 None。
    """
    if not _pystray_available():
        logger.warning("托盘需要 pystray：pip install 'coara[desktop]' 或 pip install pystray")
        return None
    launcher = TrayLauncher(
        open_web=open_web,
        open_mobile=open_mobile,
        quit_kernel=quit_kernel,
    )
    thread = threading.Thread(target=launcher.run, name="coara-tray", daemon=True)
    launcher._thread = thread
    thread.start()
    return launcher
