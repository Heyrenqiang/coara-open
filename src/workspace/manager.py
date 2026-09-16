"""Workspace manager: registry + mounts + active context."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.core.coara_home import resolve_coara_home
from src.core.logger import logger
from src.workspace.registry import WorkspaceRegistry, open_registry, registry_path
from src.workspace.types import WorkspaceEntry, WorkspaceStatus
from src.workspace.vfs import VfsResolver


class WorkspaceManager:
    """Owns workspace registry, VFS mounts, and permission checks for Root."""

    def __init__(
        self,
        initial_path: Path,
        *,
        coara_home: Path | None = None,
        workspace_alias: str | None = None,
    ):
        self.initial_path = initial_path.expanduser().resolve()
        self.startup_cwd = self.initial_path
        self.coara_home = resolve_coara_home(self.initial_path, coara_home)
        self.requested_workspace = workspace_alias
        self.registry = open_registry(self.initial_path, coara_home)
        self.vfs = VfsResolver()
        self._active_id: str | None = None
        self._registry_path = registry_path(self.coara_home)
        self._registry_mtime: float = 0
        # Optional hook: (action, entry) where action ∈ added/removed/renamed.
        # Root injects this to fan out workspace_registry_changed events.
        self.on_registry_changed: Callable[[str, WorkspaceEntry | None], None] | None = None

    async def initialize(self) -> None:
        entry = self.registry.ensure_workspace(self.initial_path)

        requested = self.requested_workspace
        requested_entry = self.registry.resolve_name_or_id(requested) if requested else None
        if requested and requested_entry is None:
            logger.warning(f"Requested workspace '{requested}' not found; using cwd workspace")
            requested = None
        # workspace_id must always be an entry.id (dict keys are entry.id).
        workspace_id = (
            requested_entry.id if requested_entry is not None else self._match_path_to_id(self.initial_path) or entry.id
        )

        for registered in self.registry.list_active():
            if not registered.resolved_path().exists():
                logger.warning(f"Workspace '{registered.name}' path missing: {registered.path}")

        self._apply_active(workspace_id)
        self._registry_mtime = self._registry_file_mtime()

    def _match_path_to_id(self, path: Path) -> str | None:
        resolved = path.expanduser().resolve()
        for entry in self.registry.document.workspaces.values():
            if entry.resolved_path() == resolved:
                return entry.id
        best_len = -1
        best_id: str | None = None
        for entry in self.registry.document.workspaces.values():
            root = entry.resolved_path()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            root_len = len(str(root))
            if root_len > best_len:
                best_len = root_len
                best_id = entry.id
        return best_id

    def cwd_display_suffix(self) -> str:
        """Relative path from active workspace root to startup cwd, if any."""
        entry = self.active_entry
        if entry is None:
            return ""
        try:
            rel = self.startup_cwd.resolve().relative_to(entry.resolved_path())
        except ValueError:
            return ""
        if not rel.parts:
            return ""
        return rel.as_posix()

    def _apply_active(self, workspace_id: str) -> None:
        active_entries = [
            entry for entry in self.registry.document.workspaces.values() if entry.status == WorkspaceStatus.ACTIVE
        ]
        if not active_entries:
            active_entries = [self.registry.ensure_workspace(self.initial_path)]
        self.vfs.set_mounts(active_entries, active_id=workspace_id)
        self._active_id = workspace_id

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def active_name(self) -> str | None:
        entry = self.active_entry
        return entry.name if entry else None

    @property
    def active_entry(self) -> WorkspaceEntry | None:
        if not self._active_id:
            return None
        return self.registry.get_by_id(self._active_id)

    @property
    def active_path(self) -> Path:
        entry = self.active_entry
        if entry is None:
            return self.initial_path
        return entry.resolved_path()

    def match_path_to_workspace_id(self, path: Path) -> str | None:
        """Resolve a filesystem path to a registered workspace id, if any."""
        return self._match_path_to_id(path)

    def name_for_path(self, path: Path | str) -> str | None:
        """Resolve a filesystem path to the registered workspace name, if any."""
        try:
            workspace_id = self._match_path_to_id(Path(path))
        except Exception:
            return None
        if not workspace_id:
            return None
        entry = self.registry.get_by_id(workspace_id)
        return entry.name if entry is not None else None

    def switch(self, workspace_id: str) -> bool:
        entry = self.registry.resolve_name_or_id(workspace_id)
        if entry is None or entry.status != WorkspaceStatus.ACTIVE:
            return False
        self._apply_active(entry.id)
        logger.info(f"Switched active workspace to {entry.name} ({entry.id})")
        return True

    def set_persistent_default(self, workspace_id: str) -> bool:
        """Persist the user's preferred default workspace (startup fallback)."""
        entry = self.registry.resolve_name_or_id(workspace_id)
        if entry is None:
            return False
        return self.registry.set_default(entry.id)

    def _notify_registry_changed(self, action: str, entry: WorkspaceEntry | None) -> None:
        callback = self.on_registry_changed
        if callback is None:
            return
        try:
            callback(action, entry)
        except Exception:
            logger.warning(f"on_registry_changed callback failed (action={action})", exc_info=True)

    def add_workspace(
        self,
        path: Path,
        *,
        name: str | None = None,
        summary: str | None = None,
    ) -> WorkspaceEntry:
        resolved = path.expanduser().resolve()
        is_new = not any(e.resolved_path() == resolved for e in self.registry.document.workspaces.values())
        entry = self.registry.ensure_workspace(path, name=name, summary=summary)
        if self._active_id:
            self._apply_active(self._active_id)
        if is_new:
            self._notify_registry_changed("added", entry)
        return entry

    def remove_workspace(self, workspace_id: str) -> bool:
        entry = self.registry.resolve_name_or_id(workspace_id)
        if entry is None:
            return False
        if entry.id == self._active_id:
            return False
        removed = self.registry.remove(entry.id)
        if removed and self._active_id:
            self._apply_active(self._active_id)
        if removed:
            self._notify_registry_changed("removed", entry)
        return removed

    def rename_workspace(self, workspace_id: str, new_name: str) -> WorkspaceEntry | None:
        """Rename a registered workspace; keep id/path. Migrate inbox alias if needed."""
        entry = self.registry.resolve_name_or_id(workspace_id)
        if entry is None:
            return None
        renamed = self.registry.rename(entry.id, new_name)
        if renamed is None:
            return None
        if self._active_id:
            self._apply_active(self._active_id)
        self._notify_registry_changed("renamed", renamed)
        return renamed

    def rebind_workspace(self, workspace_id: str, new_path: str) -> WorkspaceEntry | None:
        """改绑目录（目录被删、项目挪位后的恢复路径）：id 不变，历史档案不断链。"""
        entry = self.registry.resolve_name_or_id(workspace_id)
        if entry is None:
            return None
        rebound = self.registry.rebind_path(entry.id, new_path)
        if rebound is None:
            return None
        if self._active_id:
            self._apply_active(self._active_id)
        self._notify_registry_changed("renamed", rebound)
        return rebound

    def list_workspaces(self) -> list[WorkspaceEntry]:
        return list(self.registry.document.workspaces.values())

    def _registry_file_mtime(self) -> float:
        try:
            return self._registry_path.stat().st_mtime
        except OSError:
            return 0

    def reload_registry(self) -> None:
        """Reload registry from disk, preserving the active workspace."""
        old_entries = dict(self.registry.document.workspaces)
        new_registry = WorkspaceRegistry(self.coara_home)
        new_registry.load()
        self.registry = new_registry
        if self._active_id and self._active_id in new_registry.document.workspaces:
            self._apply_active(self._active_id)
        else:
            # active 被外部删除（或尚无 active）：清空挂载快照并解除 active，
            # 否则已注销空间残留可解析/可写直至下次 switch，且同名 id 日后
            # 重新注册会被下次 reload 静默“复活”为 active
            self.vfs.clear_mounts()
            self._active_id = None
        self._registry_mtime = self._registry_file_mtime()
        logger.debug("Registry reloaded from disk")
        self._notify_registry_diff(old_entries, new_registry.document.workspaces)

    def _notify_registry_diff(
        self,
        old_entries: dict[str, WorkspaceEntry],
        new_entries: dict[str, WorkspaceEntry],
    ) -> None:
        """Emit change notifications for entries added/removed/renamed via disk reload."""
        if self.on_registry_changed is None:
            return
        for workspace_id, entry in new_entries.items():
            old = old_entries.get(workspace_id)
            if old is None:
                self._notify_registry_changed("added", entry)
            elif old.name != entry.name:
                self._notify_registry_changed("renamed", entry)
        for workspace_id, entry in old_entries.items():
            if workspace_id not in new_entries:
                self._notify_registry_changed("removed", entry)

    def reload_if_stale(self) -> bool:
        """Reload registry if the on-disk file has changed since last load."""
        try:
            mtime = self._registry_path.stat().st_mtime
        except OSError:
            return False
        if mtime != self._registry_mtime:
            self.reload_registry()
            return True
        return False


def _migrate_workspace_alias_side_effects(coara_home: Path, old_name: str, new_name: str) -> None:
    """Best-effort migrate name-keyed side data (inbox).

    inbox 已改为按工作空间目录存储（``<workspace>/.coara/inbox/``），rename 不改目录，
    无需迁移；本函数保留为空壳以防调用点误用（2026-08 起无调用）。
    """
    return
