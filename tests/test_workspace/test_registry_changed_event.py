"""Tests for WorkspaceManager.on_registry_changed notifications (F1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.manager import WorkspaceManager


@pytest.fixture
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    # registry.py imports the function by name; patch it in the registry
    # namespace so tmp_path workspaces actually persist to disk.
    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    return WorkspaceManager(tmp_path, coara_home=tmp_path / "home")


@pytest.mark.asyncio
async def test_add_workspace_notifies(manager: WorkspaceManager) -> None:
    await manager.initialize()
    events: list[tuple[str, str | None]] = []
    manager.on_registry_changed = lambda action, entry: events.append((action, getattr(entry, "name", None)))

    target = manager.initial_path.parent / "new-ws"
    target.mkdir(exist_ok=True)
    manager.add_workspace(target, name="new-ws")
    assert ("added", "new-ws") in events


@pytest.mark.asyncio
async def test_add_workspace_idempotent_no_duplicate_notify(manager: WorkspaceManager) -> None:
    await manager.initialize()
    events: list[str] = []
    manager.on_registry_changed = lambda action, entry: events.append(action)

    target = manager.initial_path.parent / "dup-ws"
    target.mkdir(exist_ok=True)
    manager.add_workspace(target, name="dup-ws")
    manager.add_workspace(target, name="dup-ws")
    assert events.count("added") == 1


@pytest.mark.asyncio
async def test_remove_workspace_notifies(manager: WorkspaceManager) -> None:
    await manager.initialize()
    events: list[tuple[str, str | None]] = []
    manager.on_registry_changed = lambda action, entry: events.append((action, getattr(entry, "name", None)))

    target = manager.initial_path.parent / "rm-ws"
    target.mkdir(exist_ok=True)
    entry = manager.add_workspace(target, name="rm-ws")
    events.clear()

    assert manager.remove_workspace(entry.id) is True
    assert ("removed", "rm-ws") in events


@pytest.mark.asyncio
async def test_rename_workspace_notifies(manager: WorkspaceManager) -> None:
    await manager.initialize()
    events: list[tuple[str, str | None]] = []
    manager.on_registry_changed = lambda action, entry: events.append((action, getattr(entry, "name", None)))

    target = manager.initial_path.parent / "ren-ws"
    target.mkdir(exist_ok=True)
    entry = manager.add_workspace(target, name="ren-ws")
    events.clear()

    renamed = manager.rename_workspace(entry.id, "ren-ws-2")
    assert renamed is not None
    assert ("renamed", "ren-ws-2") in events


@pytest.mark.asyncio
async def test_reload_registry_detects_external_removal(manager: WorkspaceManager) -> None:
    """Cross-process CLI removal is picked up by reload + diff notification."""
    await manager.initialize()
    target = manager.initial_path.parent / "ext-ws"
    target.mkdir(exist_ok=True)
    entry = manager.add_workspace(target, name="ext-ws")

    events: list[tuple[str, str | None]] = []
    manager.on_registry_changed = lambda action, e: events.append((action, getattr(e, "name", None)))

    # Simulate another process removing the entry directly from the registry file.
    from src.workspace.registry import WorkspaceRegistry

    other = WorkspaceRegistry(manager.coara_home)
    other.load()
    assert other.remove(entry.id) is True

    changed = manager.reload_if_stale()
    assert changed is True
    assert ("removed", "ext-ws") in events
