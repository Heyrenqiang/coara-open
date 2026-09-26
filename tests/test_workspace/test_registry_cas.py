"""P0-2: workspaces.yaml compare-and-swap — concurrent writers must not clobber."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.registry import WorkspaceRegistry, WorkspaceRegistryConflictError


@pytest.fixture
def durable_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Registry persists under *home*; tmp workspace paths are treated as durable."""
    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_stale_save_raises_instead_of_clobber(durable_home: Path, tmp_path: Path) -> None:
    """两进程同读后先后改写：后写者必须冲突失败，先写者的改名不得被抹掉。"""
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()

    seed = WorkspaceRegistry(durable_home)
    seed.load()
    seed.ensure_workspace(ws1, name="ws1")
    seed.ensure_workspace(ws2, name="ws2")

    writer_a = WorkspaceRegistry(durable_home)
    writer_a.load()
    writer_b = WorkspaceRegistry(durable_home)
    writer_b.load()

    renamed_a = writer_a.rename("ws1", "alpha")
    assert renamed_a is not None
    assert renamed_a.name == "alpha"

    with pytest.raises(WorkspaceRegistryConflictError, match="其他进程"):
        writer_b.rename("ws2", "beta")

    disk = WorkspaceRegistry(durable_home)
    disk.load()
    names = {e.name for e in disk.document.workspaces.values()}
    assert "alpha" in names
    assert "ws2" in names
    assert "beta" not in names


def test_reload_then_save_succeeds_after_conflict(durable_home: Path, tmp_path: Path) -> None:
    """冲突后重新 load，可继续改写且两侧变更都在。"""
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()

    seed = WorkspaceRegistry(durable_home)
    seed.load()
    seed.ensure_workspace(ws1, name="ws1")
    seed.ensure_workspace(ws2, name="ws2")

    a = WorkspaceRegistry(durable_home)
    a.load()
    b = WorkspaceRegistry(durable_home)
    b.load()

    a.rename("ws1", "alpha")
    with pytest.raises(WorkspaceRegistryConflictError):
        b.rename("ws2", "beta")

    b.load()
    renamed = b.rename("ws2", "beta")
    assert renamed is not None and renamed.name == "beta"

    disk = WorkspaceRegistry(durable_home)
    disk.load()
    names = {e.name for e in disk.document.workspaces.values()}
    assert names == {"alpha", "beta"}


def test_fresh_registry_save_sets_token(durable_home: Path, tmp_path: Path) -> None:
    """空表首次 save 后 token 与磁盘一致，再次同实例 save 不误报冲突。"""
    ws = tmp_path / "alone"
    ws.mkdir()
    reg = WorkspaceRegistry(durable_home)
    reg.load()
    reg.ensure_workspace(ws, name="alone")
    # second mutation on same instance (token refreshed by first save)
    reg.rename("alone", "solo")
    disk = WorkspaceRegistry(durable_home)
    disk.load()
    assert disk.resolve_name_or_id("solo") is not None
