"""开机自启：把 ``coara tray`` 注册进系统登录启动项（可配置开关）"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WINDOWS_VALUE_NAME = "coara"
_WINDOWS_LAUNCHER_REL = Path("coara") / "autostart-tray.cmd"
_LINUX_DESKTOP_DIR = ".config/autostart"
_LINUX_DESKTOP_FILE = "coara.desktop"


def _source_tree_root() -> Path | None:
    """源码树根（含 ``src/coara``）。site-packages 安装形态返回 None。"""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "src" / "coara").is_dir():
        return candidate
    return None


def _windows_module_launcher_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / _WINDOWS_LAUNCHER_REL


def _ensure_windows_module_launcher_script(root: Path) -> Path:
    r"""写 ``%LOCALAPPDATA%\coara\autostart-tray.cmd``：先 cd 仓库根再起 tray"""
    script = _windows_module_launcher_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f'@echo off\r\ncd /d "{root}"\r\n"{sys.executable}" -m src.coara tray\r\n',
        encoding="utf-8",
    )
    return script


def _module_fallback_command(*, ensure_script: bool = False) -> str:
    """无独立启动器时：``python -m src.coara tray``，并尽量钉死工作目录为仓库根"""
    root = _source_tree_root()
    if sys.platform == "win32" and root is not None:
        if ensure_script:
            return f'"{_ensure_windows_module_launcher_script(root)}"'
        return f'"{_windows_module_launcher_path()}"'
    return f'"{sys.executable}" -m src.coara tray'


def _launcher_command(*, ensure_script: bool = False) -> str:
    """当前在用的 coara 启动器命令（tray 模式）"""
    exe_name = "coara.exe" if sys.platform == "win32" else "coara"
    py_dir = Path(sys.executable).resolve().parent
    for launcher in (py_dir / exe_name, py_dir / "Scripts" / exe_name):
        if launcher.is_file():
            return f'"{launcher}" tray'
    return _module_fallback_command(ensure_script=ensure_script)


def is_enabled() -> bool:
    if sys.platform == "win32":
        return _windows_is_enabled()
    if sys.platform.startswith("linux"):
        return _desktop_file().is_file()
    return False


def enable() -> str:
    """注册开机自启，返回面向用户的结果描述。"""
    cmd = _launcher_command(ensure_script=True)
    if sys.platform == "win32":
        _windows_set(cmd)
        return f"已开启开机自启（登录后自动拉起 coara 托盘）：{cmd}"
    if sys.platform.startswith("linux"):
        _write_desktop_file(cmd)
        return f"已开启开机自启（登录后自动拉起 coara 托盘）：{_desktop_file()}"
    return "当前平台不支持开机自启"


def disable() -> str:
    if sys.platform == "win32":
        removed = _windows_delete()
    elif sys.platform.startswith("linux"):
        removed = _delete_desktop_file()
    else:
        return "当前平台不支持开机自启"
    return "已关闭开机自启" if removed else "开机自启本就未开启"


def status_text() -> str:
    if is_enabled():
        return f"开机自启：已开启（{_launcher_command()}）"
    return "开机自启：未开启（coara autostart on 开启）"


def _windows_is_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY) as key:
            winreg.QueryValueEx(key, _WINDOWS_VALUE_NAME)
            return True
    except OSError:
        return False


def _windows_set(cmd: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY) as key:
        winreg.SetValueEx(key, _WINDOWS_VALUE_NAME, 0, winreg.REG_SZ, cmd)


def _windows_delete() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, access=winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _WINDOWS_VALUE_NAME)
            return True
    except OSError:
        return False


def _desktop_file() -> Path:
    return Path.home() / _LINUX_DESKTOP_DIR / _LINUX_DESKTOP_FILE


def _write_desktop_file(cmd: str) -> None:
    path = _desktop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=coara",
        f"Exec={cmd}",
        "Terminal=false",
        "X-GNOME-Autostart-enabled=true",
    ]
    root = _source_tree_root()
    if root is not None:
        lines.insert(4, f"Path={root}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _delete_desktop_file() -> bool:
    path = _desktop_file()
    if path.is_file():
        path.unlink()
        return True
    return False
