"""json_store 原子写错误可诊断性测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.json_store import write_text_atomic


def test_write_text_atomic_normal_path(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "data.txt"
    write_text_atomic(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_text_atomic_target_is_directory(tmp_path: Path) -> None:
    """目标是目录时抛出带清晰信息的 IsADirectoryError（经重试后），临时文件被清理。"""
    target = tmp_path / "session_state.json"
    target.mkdir()
    with pytest.raises(IsADirectoryError, match="目录"):
        write_text_atomic(target, "{}")
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_text_atomic_retry_exhausted_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.replace 持续 PermissionError 且目标非目录时，抛带路径信息的 PermissionError。"""
    import src.core.json_store as js

    def deny(_src: Path, _dst: Path) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(js.os, "replace", deny)
    monkeypatch.setattr(js.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError, match="不可写"):
        write_text_atomic(tmp_path / "f.json", "{}")
