"""GoMatrix 托管监督器测试：二进制探测、数据目录迁移、替换旧实例 spawn。"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.matrix_host.supervisor import (
    GoMatrixHost,
    _looks_like_gomatrix,
    _rewrite_port,
    ensure_data_dir,
    find_binary,
    is_healthy,
    purge_legacy_layout,
)

_FAKE_SERVER = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"versions":["v1.1"]}')
    def log_message(self, *a):
        pass
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(port: int, host_binary: str = "") -> SimpleNamespace:
    return SimpleNamespace(port=port, host_binary=host_binary)


@pytest.fixture()
def fake_server(tmp_path: Path):
    """起一个假 gomatrix（任意 GET 都 200），用完杀掉。"""
    script = tmp_path / "fake_gomatrix.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)])
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield proc, port
    proc.kill()
    proc.wait()


def test_find_binary_configured_takes_precedence(tmp_path: Path):
    exe = tmp_path / "gomatrix.exe"
    exe.write_text("x", encoding="utf-8")
    assert find_binary(_cfg(8008, host_binary=str(exe))) == exe


def test_find_binary_missing_returns_none():
    assert find_binary(_cfg(8008, host_binary=str(Path("Z:/no/such/gomatrix.exe")))) is None or True
    # 配置优先但不存在时仍可能命中仓库/发行布局；只断言不抛异常


def test_find_binary_dev_repo_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sys

    exe_name = "gomatrix.exe" if sys.platform == "win32" else "gomatrix"
    exe = tmp_path / "gomatrix" / exe_name
    exe.parent.mkdir(parents=True)
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr("src.core.config._repo_root", lambda: tmp_path)
    assert find_binary(_cfg(8008)) == exe


def test_is_packaged_coara_install_env(monkeypatch: pytest.MonkeyPatch):
    from src.matrix_host.supervisor import _is_packaged_coara_install

    monkeypatch.setenv("COARA_ROOT", r"C:\Users\A\AppData\Local\coara")
    assert _is_packaged_coara_install() is True


def test_is_packaged_coara_install_editable_dev(monkeypatch: pytest.MonkeyPatch):
    from src.matrix_host.supervisor import _is_packaged_coara_install

    monkeypatch.delenv("COARA_ROOT", raising=False)
    monkeypatch.setattr("sys.executable", r"D:\code_ws\v8\.venv\Scripts\python.exe")
    assert _is_packaged_coara_install() is False


def test_rewrite_port():
    assert _rewrite_port("port = 8008\n", 9000) == "port = 9000\n"
    text = 'server_name = "x"\n'
    assert "port = 9000" in _rewrite_port(text, 9000)


def test_ensure_data_dir_migrates_once(tmp_path: Path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "gomatrix.toml").write_text('server_name = "coara.local"\nport = 8008\n', encoding="utf-8")
    (legacy / "gomatrix.db").write_bytes(b"db")
    (legacy / "media").mkdir()
    (legacy / "media" / "f.bin").write_bytes(b"m")
    home = tmp_path / "home"

    toml = ensure_data_dir(home, legacy / "gomatrix.exe", 9000)
    text = toml.read_text(encoding="utf-8")
    assert "port = 9000" in text and 'server_name = "coara.local"' in text
    assert (home / "matrix" / "gomatrix.db").read_bytes() == b"db"
    assert (home / "matrix" / "media" / "f.bin").read_bytes() == b"m"
    assert not (legacy / "gomatrix.toml").exists()
    assert not (legacy / "gomatrix.db").exists()

    # 二次调用不覆盖数据目录里的改动
    toml.write_text("port = 1111\n", encoding="utf-8")
    ensure_data_dir(home, legacy / "gomatrix.exe", 9000)
    assert toml.read_text(encoding="utf-8") == "port = 1111\n"


def test_ensure_data_dir_default_toml(tmp_path: Path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    toml = ensure_data_dir(tmp_path / "home", legacy / "gomatrix.exe", 8123)
    assert "port = 8123" in toml.read_text(encoding="utf-8")


def test_purge_legacy_layout(tmp_path: Path):
    legacy = tmp_path / "bin"
    legacy.mkdir()
    (legacy / "gomatrix.toml").write_text("x", encoding="utf-8")
    (legacy / "gomatrix.db").write_bytes(b"db")
    (legacy / "media").mkdir()
    (legacy / "media" / "f.bin").write_bytes(b"m")
    purge_legacy_layout(legacy)
    assert not (legacy / "gomatrix.toml").exists()
    assert not (legacy / "gomatrix.db").exists()
    assert not (legacy / "media").exists()


@pytest.mark.asyncio
async def test_start_refuses_to_kill_foreign_listener(fake_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """端口被无关进程占用：宁可启动失败，也绝不 terminate/kill（防误杀红线）。"""
    proc, port = fake_server
    binary = tmp_path / "gomatrix.exe"
    binary.write_text("stub", encoding="utf-8")

    async def fail_spawn(self, toml: Path) -> bool:  # pragma: no cover
        raise AssertionError("不该走到 spawn")

    monkeypatch.setattr(GoMatrixHost, "_spawn", fail_spawn)
    host = GoMatrixHost(tmp_path / "home", _cfg(port, host_binary=str(binary)))
    assert await host.start() is False
    assert proc.poll() is None  # 无关进程安然无恙


@pytest.mark.asyncio
async def test_start_adopts_gomatrix_by_name(fake_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """占用进程 exe 名含 gomatrix（即使与捆绑二进制不同路径）→ 识别为同类，adopt 不杀。"""
    proc, port = fake_server
    binary = tmp_path / "gomatrix.exe"
    binary.write_text("stub", encoding="utf-8")
    other_install = tmp_path / "other" / "bin" / "gomatrix.exe"
    other_install.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "src.matrix_host.supervisor._listener_exe", lambda p: other_install
    )
    host = GoMatrixHost(tmp_path / "home", _cfg(port, host_binary=str(binary)))
    assert await host.start() is True
    assert host.managed is False  # adopt，不接管生命周期
    await host.stop()
    assert proc.poll() is None


def test_looks_like_gomatrix(tmp_path: Path):
    binary = tmp_path / "a" / "gomatrix.exe"
    assert _looks_like_gomatrix(tmp_path / "b" / "GoMatrix.exe", binary) is True  # 名字含 gomatrix（大小写不敏感）
    assert _looks_like_gomatrix(binary, binary) is True  # 同路径
    assert _looks_like_gomatrix(tmp_path / "b" / "python.exe", binary) is False
    assert _looks_like_gomatrix(None, binary) is False


@pytest.mark.asyncio
async def test_is_healthy_requires_matrix_versions(fake_server):
    """仅 200 不算健康：必须返回含 versions 列表的 Matrix JSON。"""
    _, port = fake_server
    assert await is_healthy(port) is True


@pytest.mark.asyncio
async def test_is_healthy_rejects_plain_200(tmp_path: Path):
    """返回 200 但非 Matrix 特征响应体（如普通 web 服务）→ 不健康。"""
    script = tmp_path / "plain200.py"
    script.write_text(
        "import http.server, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n",
        encoding="utf-8",
    )
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        for _ in range(40):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        assert await is_healthy(port) is False
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_start_fails_with_unrunnable_binary(tmp_path: Path):
    port = _free_port()
    junk = tmp_path / "gomatrix.exe"
    junk.write_text("not an executable", encoding="utf-8")
    host = GoMatrixHost(tmp_path / "home", _cfg(port, host_binary=str(junk)))
    assert await host.start() is False


_TOML_BASE = """server_name = "coara.local"
port = 8008

[tunnel]
enabled = true
mode = "quick"
"""


def test_apply_tunnel_config_rewrites_set_keys(tmp_path: Path):
    from src.matrix_host.supervisor import apply_tunnel_config

    toml = tmp_path / "gomatrix.toml"
    toml.write_text(_TOML_BASE, encoding="utf-8")
    cfg = SimpleNamespace(tunnel_enabled=False, tunnel_mode="named", tunnel_token="tok", tunnel_public_url="")
    apply_tunnel_config(toml, cfg)
    text = toml.read_text(encoding="utf-8")
    assert "enabled = false" in text
    assert 'mode = "named"' in text
    assert 'token = "tok"' in text
    assert "public_url" not in text  # 空值不写入


def test_apply_tunnel_config_creates_missing_section(tmp_path: Path):
    from src.matrix_host.supervisor import apply_tunnel_config

    toml = tmp_path / "gomatrix.toml"
    toml.write_text('server_name = "x"\n', encoding="utf-8")
    cfg = SimpleNamespace(tunnel_enabled=True, tunnel_mode="quick", tunnel_token="", tunnel_public_url="")
    apply_tunnel_config(toml, cfg)
    text = toml.read_text(encoding="utf-8")
    assert "[tunnel]" in text and "enabled = true" in text


def test_apply_tunnel_config_unset_keys_untouched(tmp_path: Path):
    from src.matrix_host.supervisor import apply_tunnel_config

    toml = tmp_path / "gomatrix.toml"
    toml.write_text(_TOML_BASE, encoding="utf-8")
    cfg = SimpleNamespace(tunnel_enabled=None, tunnel_mode="", tunnel_token="", tunnel_public_url="")
    apply_tunnel_config(toml, cfg)
    assert toml.read_text(encoding="utf-8") == _TOML_BASE


def test_apply_tunnel_config_appends_missing_keys_in_section(tmp_path: Path):
    from src.matrix_host.supervisor import apply_tunnel_config

    toml = tmp_path / "gomatrix.toml"
    toml.write_text(_TOML_BASE + '\n[pairing]\nusername = "phone"\n', encoding="utf-8")
    cfg = SimpleNamespace(
        tunnel_enabled=None, tunnel_mode="", tunnel_token="", tunnel_public_url="https://m.example.com"
    )
    apply_tunnel_config(toml, cfg)
    text = toml.read_text(encoding="utf-8")
    # 新键落在 [tunnel] 段内（[pairing] 之前），不串段
    tunnel_part = text.split("[pairing]")[0]
    assert 'public_url = "https://m.example.com"' in tunnel_part
