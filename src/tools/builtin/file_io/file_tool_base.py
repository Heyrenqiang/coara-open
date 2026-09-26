"""Shared base classes for workspace-bound file tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.tool_base import ToolResult
from src.tools.builtin.file_io.file_support import (
    FileReadState,
    normalize_read_state_store,
    resolve_workspace_path_from_root,
)
from src.tools.builtin.file_io.workspace_tool_base import WorkspaceBoundTool, WorkspaceBoundToolInvocation


class WorkspaceBoundFileTool(WorkspaceBoundTool):
    """Base tool for file operations bound to a workspace root and snapshot store."""

    normalize_read_states = False
    #: Write-class tools set True: out-of-mount paths declare approval instead
    #: of being hard-denied. Read-class tools stay silent (they allow any path).
    prompts_for_out_of_mount_write = False

    def __init__(
        self,
        read_state_store: dict[str, FileReadState] | None = None,
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        if self.normalize_read_states:
            read_state_store = normalize_read_state_store(read_state_store)
        self._read_states = read_state_store
        self._vfs_resolver = vfs_resolver
        super().__init__(workspace_root)

    def get_invocation_args(self) -> tuple[Any, ...]:
        return (self._read_states, self._workspace_root, self._vfs_resolver)

    def requires_approval(self, arguments: dict[str, Any]) -> bool:
        """Out-of-mount write/edit/delete goes through the approval gate."""
        if not self.prompts_for_out_of_mount_write or not isinstance(arguments, dict):
            return False
        from src.tools.builtin.file_io.file_support import path_outside_workspace_mounts

        for key in ("path", "template_path"):
            if path_outside_workspace_mounts(self._vfs_resolver, self._workspace_root, arguments.get(key)):
                return True
        return False


class WorkspaceBoundFileToolInvocation(WorkspaceBoundToolInvocation):
    """Base invocation with shared workspace/snapshot context and required-param checks."""

    required_params: tuple[str, ...] = ()
    normalize_read_states = False
    path_read_only: bool = False

    def __init__(
        self,
        params: dict[str, Any],
        read_state_store: dict[str, FileReadState] | None = None,
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        if self.normalize_read_states:
            read_state_store = normalize_read_state_store(read_state_store)
        self._read_states = read_state_store
        self._vfs_resolver = vfs_resolver
        super().__init__(params, workspace_root)
        self._validate_required_params()

    def _validate_required_params(self) -> None:
        for param_name in self.required_params:
            if param_name not in self.params:
                raise ValueError(f"Missing required parameter: {param_name}")

    def _approval_baseline_error(self) -> ToolResult | None:
        """Refuse execute when the approval-window file baseline no longer matches."""
        from src.agent.approval_baseline import verify_approval_file_baseline

        drift = verify_approval_file_baseline(self)
        if drift:
            return ToolResult.error(drift)
        return None

    def _resolve_file_path_or_error(
        self,
        file_path: str,
        operation: str,
        *,
        allow_missing: bool = False,
        allow_non_file: bool = False,
        required_suffix: str | None = None,
    ) -> tuple[Path | None, str | None]:
        """Resolve file_path against workspace root. Returns (resolved_path, error_message)."""
        path, _, path_error = resolve_workspace_path_from_root(
            file_path,
            self._workspace_root,
            operation,
            vfs_resolver=self._vfs_resolver,
            read_only=self.path_read_only,
        )
        if path_error:
            return None, path_error
        if path is None:
            return None, "路径解析失败"
        if required_suffix is not None and path.suffix != required_suffix:
            return None, f"文件必须是 {required_suffix} 格式: {path}"
        if not allow_missing:
            if not path.exists():
                return None, f"文件不存在: {path}"
            if not allow_non_file and not path.is_file():
                return None, f"目标不是一个文件: {path}"
        else:
            if not allow_non_file and path.exists() and not path.is_file():
                return None, f"目标已存在且不是一个文件: {path}"
        return path, None
