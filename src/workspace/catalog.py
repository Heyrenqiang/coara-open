"""Workspace catalog helpers for workspace listing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.workspace.manager import WorkspaceManager
from src.workspace.types import WorkspaceEntry, WorkspaceStatus


def resolve_workspace_summary(entry: WorkspaceEntry) -> str:
    """One-line summary for list display."""
    return entry.summary.strip() or "（无摘要）"


def resolve_foreground_active_name(root: Any, manager: WorkspaceManager) -> str | None:
    """Resolve active workspace name from the foreground session workspace path."""
    fg = root.foreground_coara if hasattr(root, "foreground_coara") else root
    fg_dir = Path(getattr(fg, "workspace_dir", manager.active_path)).expanduser().resolve()
    workspace_id = manager.match_path_to_workspace_id(fg_dir)
    if workspace_id is None:
        return manager.active_name
    entry = manager.registry.get_by_id(workspace_id)
    return entry.name if entry else manager.active_name


def resolve_foreground_active_id(root: Any, manager: WorkspaceManager) -> str | None:
    """Resolve active workspace id from the foreground session workspace path."""
    fg = root.foreground_coara if hasattr(root, "foreground_coara") else root
    fg_dir = Path(getattr(fg, "workspace_dir", manager.active_path)).expanduser().resolve()
    return manager.match_path_to_workspace_id(fg_dir) or manager.active_id


def format_workspace_catalog(
    manager: WorkspaceManager,
    *,
    active_name: str | None = None,
) -> str:
    """Plain-text catalog for ws(action=list).

    *active_name* overrides ``manager.active_name`` when the foreground session
    workspace and the global registry pointer are out of sync.
    """
    entries = [e for e in manager.list_workspaces() if e.status == WorkspaceStatus.ACTIVE]
    entries.sort(key=lambda e: e.name)
    if not entries:
        return "# 已登记工作空间\n\n（暂无）纳入管理：`ws(action=add, name=..., path=...)`。"

    resolved_active = active_name if active_name is not None else manager.active_name
    lines = ["# 已登记工作空间", ""]
    for entry in entries:
        flag = " **(当前)**" if entry.name == resolved_active else ""
        lines.append(f"## {entry.name}{flag}")
        lines.append(f"- **路径**：{entry.path}")
        lines.append(f"- **摘要**：{resolve_workspace_summary(entry)}")
        lines.append("")
    return "\n".join(lines).rstrip()
