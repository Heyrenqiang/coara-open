"""workspace_state 持久化失败降级测试。"""

from __future__ import annotations

import json
from pathlib import Path

from src.coara.workspace_state import load_session_state, save_session_state


def test_save_session_state_normal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    save_session_state(ws, "s1", coara_home=home)
    sid, last = load_session_state(ws, coara_home=home)
    assert sid == "s1"
    assert last > 0


def test_save_session_state_failure_degrades_to_warning(tmp_path: Path) -> None:
    """目标路径是目录时原子写必失败：降级为 warning，不抛穿，无 tmp 残留。"""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    save_session_state(ws, "s1", coara_home=home)
    state_file = home / "workspaces" / next((home / "workspaces").iterdir()).name / "session_state.json"
    state_file.unlink()
    state_file.mkdir()  # 目标变目录 → write_text_atomic 抛 IsADirectoryError

    save_session_state(ws, "s2", coara_home=home)  # 不抛异常
    assert not list(state_file.parent.rglob("*.tmp"))
    # 目录仍在，数据未写入
    assert state_file.is_dir()


def test_save_session_state_content_via_atomic(tmp_path: Path) -> None:
    """落盘内容与旧 tmp+replace 路径一致（含缩进 JSON）。"""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    save_session_state(ws, "s1", coara_home=home, last_updated=123.0)
    sid, last = load_session_state(ws, coara_home=home)
    assert (sid, last) == ("s1", 123.0)
    state_file = home / "workspaces" / next((home / "workspaces").iterdir()).name / "session_state.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["session_id"] == "s1"
