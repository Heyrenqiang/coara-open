"""竞态治理：publish_active_runtime 不覆盖另一个仍存活主进程的登记。

根因（实测）：两个 coara 几乎同时启动，后死者的 active.json 盖过先活者，
导致「活的是 11084、active.json 却记着已死的 33868」，判据随之失真。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.coara import workspace_runtime as rt


def _publish(home: Path, workspace: Path) -> None:
    rt.publish_active_runtime(
        coara_home=home,
        workspace_path=workspace,
        workspace_name="ws",
        session_id="s",
        coara_id="c",
        coara_name="n",
    )


def _write_active(home: Path, pid: int) -> None:
    target = rt.runtime_file(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "workspace_id": "ws-x",
                "workspace_path": "/x",
                "workspace_name": "x",
                "pid": pid,
                "session_id": "s",
                "coara_id": "c",
                "coara_name": "n",
                "started_at": "t",
            }
        ),
        encoding="utf-8",
    )


def test_publish_refuses_to_override_live_other_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    # active.json 登记着一个 别的活进程 pid
    _write_active(home, pid=99999)
    monkeypatch.setattr(rt, "is_pid_alive", lambda pid: pid == 99999)

    _publish(home, ws)

    # 未被覆盖：pid 仍是 99999，不是当前进程
    payload = json.loads(rt.runtime_file(home).read_text(encoding="utf-8"))
    assert payload["pid"] == 99999


def test_publish_overrides_own_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一主进程切空间：active.json 里是自己 pid，正常覆盖。"""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_active(home, pid=os.getpid())
    monkeypatch.setattr(rt, "is_pid_alive", lambda pid: True)

    _publish(home, ws)

    payload = json.loads(rt.runtime_file(home).read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["workspace_name"] == "ws"


def test_publish_overrides_dead_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """active.json 是死 pid 残留：正常覆盖（清掉残留）。"""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_active(home, pid=99999)
    monkeypatch.setattr(rt, "is_pid_alive", lambda pid: False)  # 全死

    _publish(home, ws)

    payload = json.loads(rt.runtime_file(home).read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()


def test_load_active_runtime_returns_none_for_dead_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """判据根基：load_active_runtime 对死 pid 返回 None（无活主进程），并清掉僵尸登记。"""
    home = tmp_path / "home"
    _write_active(home, pid=99999)
    monkeypatch.setattr(rt, "is_pid_alive", lambda pid: False)
    assert rt.load_active_runtime(home) is None
    assert not rt.runtime_file(home).exists()

    _write_active(home, pid=99999)
    monkeypatch.setattr(rt, "is_pid_alive", lambda pid: True)
    running = rt.load_active_runtime(home)
    assert running is not None and running.pid == 99999
