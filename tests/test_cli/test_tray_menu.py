"""Tray menu: open web / mobile / quit; no status or restart filler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import pystray  # noqa: F401
except Exception as exc:  # Xlib DisplayNameError 等：Linux 无头环境 import 即炸
    pytest.skip(f"pystray 不可用（无显示环境？）: {exc}", allow_module_level=True)


def test_tray_menu_has_quit_not_restart_or_status() -> None:
    from src.cli.tray import TrayLauncher

    launcher = TrayLauncher(
        open_web=MagicMock(),
        open_mobile=MagicMock(),
        quit_kernel=MagicMock(),
    )
    labels: list[str] = []
    for item in launcher._menu():
        text = getattr(item, "text", None)
        if callable(text):
            labels.append(str(text(SimpleNamespace())))
        elif text is not None:
            labels.append(str(text))
    assert "打开 Web 界面" in labels
    assert "退出 coara" in labels
    assert "重启 coara" not in labels
    assert not any("运行中" in label for label in labels)
