"""Glob tool — fast file path search by pattern."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.search_support import (
    GLOB_TRUNCATION_HINT,
    MAX_RESULTS_CAP,
    WALK_TRUNCATION_HINT,
    WalkStatus,
    append_truncation_footer,
    iter_glob_paths,
    resolve_search_root,
    resolve_workspace_search_root,
    validate_search_directory,
)
from src.tools.builtin.file_io.workspace_tool_base import WorkspaceBoundTool, WorkspaceBoundToolInvocation


class GlobToolInvocation(WorkspaceBoundToolInvocation):
    """Invocation for glob file search."""

    def __init__(
        self,
        params: dict[str, Any],
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        super().__init__(params, workspace_root)
        self._vfs_resolver = vfs_resolver
        if "pattern" not in params:
            raise ValueError("Missing required parameter: pattern")
        self.root = resolve_search_root(params, self._workspace_root)
        self.pattern = str(params["pattern"])

    def get_description(self) -> str:
        return f"Glob {self.pattern} @ {self.root}"

    async def execute(self, signal=None) -> ToolResult:
        root, path_error = resolve_workspace_search_root(
            self.root,
            self._workspace_root,
            "Glob",
            vfs_resolver=self._vfs_resolver,
        )
        if path_error:
            return ToolResult.error(path_error)
        assert root is not None

        dir_error = validate_search_directory(root)
        if dir_error:
            return ToolResult.error(dir_error)

        # os.walk does blocking filesystem IO; run it in a thread to keep the event loop free.
        matches, truncated, walk_truncated = await asyncio.to_thread(self._collect_matches, root)

        if not matches:
            meta = {"root": str(root), "count": 0, "truncated": False, "walk_truncated": walk_truncated}
            content = "未找到匹配文件"
            if walk_truncated:
                content = f"{content}\n{WALK_TRUNCATION_HINT}"
                meta["truncated"] = True
            return ToolResult.success(content, metadata=meta)

        matches = sorted(matches[:MAX_RESULTS_CAP])
        if truncated or walk_truncated:
            append_truncation_footer(
                matches,
                shown=len(matches),
                total=None,
                offset=0,
                hint=WALK_TRUNCATION_HINT if walk_truncated and not truncated else GLOB_TRUNCATION_HINT,
            )

        return ToolResult.success(
            "\n".join(matches),
            metadata={
                "root": str(root),
                "count": len(matches),
                "truncated": truncated or walk_truncated,
                "walk_truncated": walk_truncated,
            },
        )

    def _collect_matches(self, root: Path) -> tuple[list[str], bool, bool]:
        matches: list[str] = []
        truncated = False
        walk_status = WalkStatus()
        for path in iter_glob_paths(root, self.pattern, kind="file", status=walk_status):
            matches.append(path.as_posix())
            if len(matches) >= MAX_RESULTS_CAP + 1:
                truncated = True
                break
        return matches, truncated, walk_status.truncated


class GlobTool(WorkspaceBoundTool):
    """Search for files matching a glob pattern."""

    name = "glob"
    description = """按 glob 查找文件并返回绝对路径，可直接用于 read/edit；自动跳过 .git、node_modules 和点目录。路径不确定时先查找；结果过多时收窄 pattern，不翻页"""  # noqa: E501
    display_name = "Glob"
    category = "filesystem"
    kind = ToolKind.SEARCH
    invocation_class = GlobToolInvocation
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob 模式，如 *.js、**/test_*.ts、src/**/*.py；不含 / 时递归匹配"
                ),
            },
            "path": {
                "type": "string",
                "description": "搜索根目录的绝对路径；省略时搜索整个工作区",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace_root: Path | None = None, vfs_resolver: Any | None = None):
        super().__init__(workspace_root)
        self._vfs_resolver = vfs_resolver

    def get_invocation_args(self) -> tuple[Any, ...]:
        return (self._workspace_root, self._vfs_resolver)
