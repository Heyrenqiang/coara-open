"""Tests for foreground-aware ws list active workspace resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.workspace.catalog import format_workspace_catalog, resolve_foreground_active_name
from src.workspace.manager import WorkspaceManager


@pytest.mark.asyncio
async def test_ws_list_marks_foreground_workspace_current_when_manager_desynced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    nx = tmp_path / "nx"
    pora = tmp_path / "pora"
    nx.mkdir()
    pora.mkdir()

    manager = WorkspaceManager(nx, coara_home=coara_home)
    await manager.initialize()
    manager.registry.ensure_workspace(nx, name="nx")
    manager.registry.ensure_workspace(pora, name="pora")
    manager.registry.save()

    manager.switch(manager.registry.ensure_workspace(nx, name="nx").id)
    assert manager.active_name == "nx"

    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(workspace_dir=pora),
    )

    active_name = resolve_foreground_active_name(root, manager)
    assert active_name == "pora"

    text = format_workspace_catalog(manager, active_name=active_name)
    assert "pora **(当前)**" in text
    assert "nx **(当前)**" not in text


@pytest.mark.asyncio
async def test_sync_workspace_manager_to_foreground_repairs_active_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    nx = tmp_path / "nx"
    pora = tmp_path / "pora"
    nx.mkdir()
    pora.mkdir()

    manager = WorkspaceManager(nx, coara_home=coara_home)
    await manager.initialize()
    nx_entry = manager.registry.ensure_workspace(nx, name="nx")
    pora_entry = manager.registry.ensure_workspace(pora, name="pora")
    manager.registry.save()

    manager.switch(nx_entry.id)
    assert manager.active_id == nx_entry.id

    from src.coara.root import RootCoara

    root = SimpleNamespace(
        workspace_manager=manager,
        foreground_coara=SimpleNamespace(workspace_dir=pora),
    )
    root.sync_workspace_manager_to_foreground = RootCoara.sync_workspace_manager_to_foreground.__get__(root, RootCoara)

    assert root.sync_workspace_manager_to_foreground() is True
    assert manager.active_id == pora_entry.id
    assert manager.active_name == "pora"
