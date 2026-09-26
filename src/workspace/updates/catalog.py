"""Build workspace updates payloads for Matrix sync (registry + unread markers)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.workspace.registry import open_registry
from src.workspace.types import WorkspaceStatus
from src.workspace.updates.store import WorkspaceUpdatesStore


def build_updates_workspaces_payload(*, coara_home: Path, workspace_dir: Path) -> list[dict[str, Any]]:
    """Registered active workspaces with per-workspace unread count and latest entry."""
    registry = open_registry(workspace_dir, coara_home)
    registry.load()
    store = WorkspaceUpdatesStore(coara_home, registry=registry)

    workspaces: list[dict[str, Any]] = []
    for workspace_id, entry in sorted(registry.document.workspaces.items()):
        if entry.status != WorkspaceStatus.ACTIVE:
            continue
        latest = store.latest(entry.name)
        workspaces.append(
            {
                "workspace_id": workspace_id,
                "name": entry.name,
                "summary": entry.summary,
                "path": entry.path,
                "unread": store.unread_count(entry.name),
                "latest": (
                    {
                        "type": latest.type,
                        "title": latest.title,
                        "created_at": latest.created_at,
                    }
                    if latest is not None
                    else None
                ),
            }
        )
    return workspaces
