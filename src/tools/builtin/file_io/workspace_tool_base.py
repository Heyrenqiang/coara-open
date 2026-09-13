"""Shared base classes for tools bound to a workspace root."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation
from src.tools.builtin.file_io.file_support import normalize_workspace_root


class WorkspaceBoundTool(BaseTool):
    """Base tool that carries a normalized workspace root into invocations."""

    invocation_class: type[ToolInvocation]

    def __init__(self, workspace_root: Path | None = None):
        super().__init__()
        self._workspace_root = normalize_workspace_root(workspace_root)

    def get_invocation_args(self) -> tuple[Any, ...]:
        return (self._workspace_root,)

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return self.invocation_class(params, *self.get_invocation_args())


class WorkspaceBoundToolInvocation(ToolInvocation):
    """Base invocation that exposes a normalized workspace root."""

    def __init__(self, params: dict[str, Any], workspace_root: Path | None = None):
        super().__init__(params)
        self._workspace_root = normalize_workspace_root(workspace_root)
