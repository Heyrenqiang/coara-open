"""开机自启测试：Windows 注册表与 Linux desktop 文件两端（平台无关桩）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.cli import autostart


@pytest.fixture()
def fake_win32(monkeypatch: pytest.MonkeyPatch):
    """把平台钉成 win32，并用内存 dict 桩掉 winreg。"""
    store: dict[str, str] = {}
    monkeypatch.setattr(sys, "platform", "win32")

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeWinreg:  # noqa: N801,N802 — 镜像 winreg API 命名
        HKEY_CURRENT_USER = 0
        REG_SZ = 1
        KEY_SET_VALUE = 2

        @staticmethod
        def OpenKey(root, path, access=0):  # noqa: N802
            return _Key()

        @staticmethod
        def CreateKey(root, path):  # noqa: N802
            return _Key()

        @staticmethod
        def QueryValueEx(key, name):  # noqa: N802
            if name not in store:
                raise OSError("not found")
            return (store[name], 1)

        @staticmethod
        def SetValueEx(key, name, _r, _t, value):  # noqa: N802
            store[name] = value

        @staticmethod
        def DeleteValue(key, name):  # noqa: N802
            if name not in store:
                raise OSError("not found")
            del store[name]

    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg)
    return store


def test_windows_enable_writes_run_key(fake_win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    launcher = tmp_path / "coara.exe"
    launcher.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert autostart.is_enabled() is False
    msg = autostart.enable()
    assert "已开启" in msg
    assert autostart.is_enabled() is True
    assert "tray" in fake_win32["coara"]
    assert "coara.exe" in fake_win32["coara"]


def test_windows_disable_removes_key(fake_win32):
    autostart.enable()
    assert autostart.disable() == "已关闭开机自启"
    assert autostart.is_enabled() is False
    assert autostart.disable() == "开机自启本就未开启"


def test_windows_status_text(fake_win32):
    assert "未开启" in autostart.status_text()
    autostart.enable()
    assert "已开启" in autostart.status_text()


def test_windows_launcher_prefers_scripts_coara(fake_win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Scripts 下有 coara.exe 时优先用它（打包安装形态）。"""
    (tmp_path / "coara.exe").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert "coara.exe" in autostart._launcher_command()


def test_windows_launcher_falls_back_to_python_module(fake_win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """没有独立启动器（开发环境）退回带 cwd 的 launcher 脚本路径；只读不写盘。"""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    cmd = autostart._launcher_command()
    assert "autostart-tray.cmd" in cmd
    script = Path(cmd.strip('"'))
    assert not script.is_file()  # status/GET 不得写盘


def test_windows_enable_writes_module_launcher_script(
    fake_win32, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """enable() 才落盘 autostart-tray.cmd。"""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    autostart.enable()
    script = Path(autostart._launcher_command().strip('"'))
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "-m src.coara tray" in body
    assert "cd /d" in body


@pytest.fixture()
def fake_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_linux_enable_writes_desktop_file(fake_linux: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / "coara").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert autostart.is_enabled() is False
    autostart.enable()
    assert autostart.is_enabled() is True
    text = (fake_linux / ".config" / "autostart" / "coara.desktop").read_text(encoding="utf-8")
    assert "Exec=" in text and "tray" in text
    assert "Terminal=false" in text
    # 源码树下应写入 Path= 钉死工作目录
    assert "Path=" in text


def test_linux_disable_deletes_desktop_file(fake_linux: Path):
    autostart.enable()
    assert autostart.disable() == "已关闭开机自启"
    assert autostart.is_enabled() is False
    assert autostart.disable() == "开机自启本就未开启"


def test_unsupported_platform(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert autostart.is_enabled() is False
    assert "不支持" in autostart.enable()
    assert "不支持" in autostart.disable()
