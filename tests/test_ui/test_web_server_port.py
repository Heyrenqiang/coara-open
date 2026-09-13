"""Tests for WebServer port-conflict detection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from src.ui.web_server import WebServer
from tests.helpers import make_test_coara


def _fake_process(pid: int = 99999, name: str = "python.exe"):
    return SimpleNamespace(pid=pid, name=lambda: name)


def _listen(port: int, pid: int):
    return SimpleNamespace(
        status=psutil.CONN_LISTEN,
        laddr=SimpleNamespace(port=port),
        pid=pid,
    )


@pytest.mark.parametrize(
    "case,expect_error",
    [
        ("other_process", True),
        ("same_process", False),
        ("free", False),
        ("access_denied", False),
    ],
)
def test_raise_if_port_in_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expect_error: bool,
) -> None:
    if case == "other_process":
        monkeypatch.setattr(psutil, "net_connections", lambda kind: [_listen(8080, 12345)])
        monkeypatch.setattr(psutil, "Process", lambda pid=None: _fake_process(pid=pid or 99999))
    elif case == "same_process":
        monkeypatch.setattr(psutil, "net_connections", lambda kind: [_listen(8080, 99999)])
        monkeypatch.setattr(psutil, "Process", lambda pid=None: _fake_process(pid=pid or 99999))
    elif case == "free":
        monkeypatch.setattr(psutil, "net_connections", lambda kind: [])
        monkeypatch.setattr(psutil, "Process", lambda pid=None: _fake_process())
    else:
        monkeypatch.setattr(
            psutil,
            "net_connections",
            lambda kind: (_ for _ in ()).throw(psutil.AccessDenied()),
        )
        monkeypatch.setattr(psutil, "Process", lambda pid=None: _fake_process())

    server = WebServer(make_test_coara(tmp_path), workspace_dir=tmp_path, port=8080)
    if expect_error:
        with pytest.raises(RuntimeError, match="端口 8080 已被 PID 12345"):
            server._raise_if_port_in_use()
    else:
        server._raise_if_port_in_use()
