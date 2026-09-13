"""Virtual path resolver with workspace mounts."""

from __future__ import annotations

from pathlib import Path

from src.core.text import absolute_path_error
from src.workspace.types import MountMode, ResolvedPath, WorkspaceEntry


class VfsResolutionError(Exception):
    """Raised when a path cannot be resolved within mounted workspaces."""


class UnmountedPathError(VfsResolutionError):
    """Raised when an absolute path lies outside every mounted workspace."""


class VfsResolver:
    """Resolve absolute paths against mount table.

    ``strict`` resolvers (delegate-scoped) hard-deny unmounted paths; the
    default resolver lets write tools fall through to the approval gate.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self._mounts: dict[str, WorkspaceEntry] = {}
        self._active_id: str | None = None
        self.strict = strict

    @property
    def active_id(self) -> str | None:
        return self._active_id

    @property
    def active_entry(self) -> WorkspaceEntry | None:
        if self._active_id is None:
            return None
        return self._mounts.get(self._active_id)

    def set_mounts(self, entries: list[WorkspaceEntry], *, active_id: str) -> None:
        self._mounts = {entry.id: entry for entry in entries}
        if active_id not in self._mounts:
            raise ValueError(f"Active workspace '{active_id}' is not mounted")
        self._active_id = active_id

    def clear_mounts(self) -> None:
        """清空挂载表：registry 外部变更使 active 失效时强制后续操作重解析。"""
        self._mounts = {}
        self._active_id = None

    def mount_for_path(self, path: Path) -> WorkspaceEntry | None:
        resolved = path.expanduser().resolve()
        best: WorkspaceEntry | None = None
        best_len = -1
        for entry in self._mounts.values():
            root = entry.resolved_path()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            root_len = len(str(root))
            if root_len > best_len:
                best = entry
                best_len = root_len
        return best

    def mount_by_name_or_id(self, workspace: str) -> WorkspaceEntry | None:
        """Look up a mounted workspace by registry name or id."""
        key = (workspace or "").strip()
        if not key:
            return None
        by_id = self._mounts.get(key)
        if by_id is not None:
            return by_id
        for entry in self._mounts.values():
            if entry.name == key:
                return entry
        return None

    def resolve_in_workspace(
        self,
        workspace: str,
        relative: str | None = None,
        *,
        action: str = "access",
    ) -> ResolvedPath:
        """Resolve ``relative`` under a mounted workspace (by name or id) to an absolute path."""
        entry = self.mount_by_name_or_id(workspace)
        if entry is None:
            raise VfsResolutionError(f"{action} 未挂载工作空间: {workspace}")
        rel = (relative or ".").strip() or "."
        if Path(rel).is_absolute():
            return self.resolve(rel, action=action)
        candidate = (entry.resolved_path() / rel).resolve()
        return self.resolve(str(candidate), action=action)

    def resolve(self, raw_path: str, *, action: str = "access") -> ResolvedPath:
        candidate = str(raw_path or "").strip()
        if not candidate:
            raise VfsResolutionError(f"{action} 路径不能为空")

        if candidate.startswith(("\\\\", "//")):
            raise VfsResolutionError(f"{action} 拒绝 UNC 路径: {raw_path}")

        if candidate.startswith(("\\\\?\\", "//?/")):
            raise VfsResolutionError(f"{action} 拒绝 Windows 扩展路径: {raw_path}")

        if ":" in candidate and len(candidate) >= 2 and candidate[1] == ":" and Path(candidate).is_absolute():
            return self._resolve_absolute(Path(candidate), action=action)

        abs_err = absolute_path_error(candidate, action)
        if abs_err:
            raise VfsResolutionError(abs_err)

        return self._resolve_absolute(Path(candidate), action=action)

    def _resolve_absolute(self, path: Path, *, action: str) -> ResolvedPath:
        target = path.expanduser().resolve()
        entry = self.mount_for_path(target)
        if entry is None:
            raise UnmountedPathError(f"{action} 路径不在任何已挂载工作空间内: {target}")
        root = entry.resolved_path()
        self._ensure_inside(root, target, action=action)
        relative = str(target.relative_to(root)).replace("\\", "/")
        return ResolvedPath(workspace_id=entry.id, path=target, relative=relative, mode=entry.mode)

    @staticmethod
    def _ensure_inside(root: Path, target: Path, *, action: str) -> None:
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise VfsResolutionError(f"{action} 拒绝工作区外路径: {target}") from exc

    def format_mount_list(self) -> str:
        if not self._mounts:
            return "(no workspaces mounted)"
        lines: list[str] = []
        for workspace_id, entry in sorted(self._mounts.items()):
            active = " (active)" if workspace_id == self._active_id else ""
            mode = "read-only" if entry.mode == MountMode.READ_ONLY else "read-write"
            lines.append(f"- {entry.name}{active}: {entry.resolved_path()} [{mode}]")
        return "\n".join(lines) if lines else "(single workspace)"
