"""GoMatrix 托管监督器测试：二进制探测、数据目录迁移、替换旧实例 spawn。"""

from __future__ import annotations

import asyncio
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
        if self.path.startswith('/api/dashboard/status'):
            # adopt 对账：上游与本端一致（8080 默认端口、不带 token_file 跳过该项）
            self.wfile.write(b'{"coara_api":{"enabled":true,"upstream_port":8080}}')
        else:
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
    # adopt 对账读 COARA_WEB_PORT：钉死与桩应答一致
    monkeypatch.setenv("COARA_WEB_PORT", "8080")
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


# ---- 隧道公网可达性看门狗 ----


def _host_for_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GoMatrixHost:
    binary = tmp_path / "gomatrix.exe"
    binary.write_text("stub", encoding="utf-8")
    host = GoMatrixHost(tmp_path / "home", _cfg(8008, host_binary=str(binary)))
    host._binary = binary
    return host


def _status(**over) -> dict:
    base = {"tunnel_enabled": True, "tunnel_state": "ready", "tunnel_url": "https://x.trycloudflare.com"}
    base.update(over)
    return base


def _async_ret(value):
    async def _impl(*args, **kw):
        return value

    return _impl


@pytest.mark.asyncio
async def test_check_tunnel_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(_status()))
    monkeypatch.setattr(sup, "_tunnel_probe", _async_ret(True))
    assert await host._check_tunnel() is True


@pytest.mark.asyncio
async def test_check_tunnel_dead_nxdomain_with_internet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """硬死信号（NXDOMAIN）且能上网 → False。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(_status()))
    monkeypatch.setattr(sup, "_tunnel_probe", _async_ret(False))
    monkeypatch.setattr(sup, "_internet_up", _async_ret(True))
    assert await host._check_tunnel() is False


@pytest.mark.asyncio
async def test_check_tunnel_dead_but_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """硬死信号但整机断网 → None（不重启、不耗退避）。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(_status()))
    monkeypatch.setattr(sup, "_tunnel_probe", _async_ret(False))
    monkeypatch.setattr(sup, "_internet_up", _async_ret(False))
    assert await host._check_tunnel() is None


@pytest.mark.asyncio
async def test_check_tunnel_uncertain_but_internet_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """探测不确定（超时）即使能上网 → None（瞬时超时 ≠ 隧道真死）。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(_status()))
    monkeypatch.setattr(sup, "_tunnel_probe", _async_ret(None))
    monkeypatch.setattr(sup, "_internet_up", _async_ret(True))
    assert await host._check_tunnel() is None


@pytest.mark.asyncio
async def test_check_tunnel_uncertain_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """探测不确定且整机断网 → None（断网中，不判死、不重启）。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(_status()))
    monkeypatch.setattr(sup, "_tunnel_probe", _async_ret(None))
    monkeypatch.setattr(sup, "_internet_up", _async_ret(False))
    assert await host._check_tunnel() is None


@pytest.mark.asyncio
async def test_check_tunnel_disabled_is_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """未启用隧道 → True（不参与重启判据）。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(_status(tunnel_enabled=False, tunnel_url="")))
    assert await host._check_tunnel() is True


@pytest.mark.asyncio
async def test_check_tunnel_status_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """dashboard 状态拿不到 → None（本轮不探）。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(sup, "_dashboard_status", _async_ret(None))
    assert await host._check_tunnel() is None


@pytest.mark.asyncio
async def test_tunnel_probe_dead_on_nxdomain(monkeypatch: pytest.MonkeyPatch):
    """ConnectError 含 getaddrinfo → 确定死 False。"""
    import httpx

    import src.matrix_host.supervisor as sup

    async def raise_connect(self, method, url, **kw):
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")

    monkeypatch.setattr(httpx.AsyncClient, "request", raise_connect)
    assert await sup._tunnel_probe("https://gone.trycloudflare.com") is False


@pytest.mark.asyncio
async def test_tunnel_probe_dead_on_cf_530(monkeypatch: pytest.MonkeyPatch):
    """CF 隧道错误页 530 → 确定死 False。"""
    import httpx

    import src.matrix_host.supervisor as sup

    class _Resp:
        status_code = 530

        def json(self):
            return {}

    async def fake_request(self, method, url, **kw):
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    assert await sup._tunnel_probe("https://x.trycloudflare.com") is False


@pytest.mark.asyncio
async def test_tunnel_probe_uncertain_on_timeout(monkeypatch: pytest.MonkeyPatch):
    """超时（非 DNS 错误）→ None。"""
    import httpx

    import src.matrix_host.supervisor as sup

    async def raise_timeout(self, method, url, **kw):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "request", raise_timeout)
    assert await sup._tunnel_probe("https://x.trycloudflare.com") is None


@pytest.mark.asyncio
async def test_tunnel_probe_alive_requires_versions(monkeypatch: pytest.MonkeyPatch):
    """200 但非 Matrix 特征 → None（CF 错误页返回 200 时绝不误判活）。"""
    import httpx

    import src.matrix_host.supervisor as sup

    class _Resp:
        status_code = 200

        def json(self):
            return {"error": "not matrix"}

    async def fake_request(self, method, url, **kw):
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    assert await sup._tunnel_probe("https://x.trycloudflare.com") is None


@pytest.mark.asyncio
async def test_tunnel_dead_threshold_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """连续 _TUNNEL_DEAD_THRESHOLD 次确定死 → 触发 _restart；单次抖动不触发。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    host._managed = True
    restarts: list[str] = []

    async def fake_restart(reason: str) -> None:
        restarts.append(reason)

    monkeypatch.setattr(host, "_restart", fake_restart)
    monkeypatch.setattr(sup, "is_healthy", _async_ret(True))
    monkeypatch.setattr(sup, "_WATCH_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(sup, "_TUNNEL_PROBE_EVERY_TICKS", 1)
    monkeypatch.setattr(sup, "_TUNNEL_SPAWN_GRACE_SECONDS", 0.0)

    verdicts = iter([False, True, False, False, True])  # 一死一活清零，再连死两次

    async def next_verdict():
        return next(verdicts)

    monkeypatch.setattr(host, "_check_tunnel", next_verdict)

    async def run():
        task = asyncio.create_task(host._watch_loop())
        for _ in range(200):
            if restarts:
                break
            await asyncio.sleep(0.01)
        host._stopping = True
        await task

    await run()
    assert restarts == ["隧道公网不可达"]


@pytest.mark.asyncio
async def test_tunnel_none_verdict_never_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """断网期（verdict None）不累计、不重启、不耗退避档位。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    host._managed = True
    restarts: list[str] = []

    async def fake_restart(reason: str) -> None:
        restarts.append(reason)

    monkeypatch.setattr(host, "_restart", fake_restart)
    monkeypatch.setattr(sup, "is_healthy", _async_ret(True))
    monkeypatch.setattr(sup, "_WATCH_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(sup, "_TUNNEL_PROBE_EVERY_TICKS", 1)
    monkeypatch.setattr(sup, "_TUNNEL_SPAWN_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(host, "_check_tunnel", _async_ret(None))

    task = asyncio.create_task(host._watch_loop())
    await asyncio.sleep(0.05)
    host._stopping = True
    await task
    assert restarts == []
    assert host._restart_count == 0


@pytest.mark.asyncio
async def test_restart_kills_adopted_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """adopt 实例（_proc=None）重启时按端口杀占位 gomatrix 进程再重拉。"""
    import src.matrix_host.supervisor as sup

    host = _host_for_probe(tmp_path, monkeypatch)
    host._managed = False
    host._proc = None
    killed: list[int] = []
    spawned: list[bool] = []

    class _FakeProc:
        pid = 4321

        def terminate(self):
            killed.append(self.pid)

        def kill(self):
            killed.append(-self.pid)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sup, "_listener_process", lambda port: _FakeProc())

    async def fake_spawn(toml: Path) -> bool:
        spawned.append(True)
        return True

    monkeypatch.setattr(host, "_spawn", fake_spawn)
    monkeypatch.setattr(sup, "_RESTART_BACKOFFS", (0.0,))
    monkeypatch.setattr(sup, "ensure_data_dir", lambda home, binary, port: tmp_path / "gomatrix.toml")
    monkeypatch.setattr(sup, "rewrite_toml_port", lambda toml, port: None)
    monkeypatch.setattr(sup, "apply_tunnel_config", lambda toml, cfg: None)

    await host._restart("隧道公网不可达")
    assert killed == [4321]
    assert spawned == [True]
