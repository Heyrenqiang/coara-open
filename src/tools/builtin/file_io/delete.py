"""Delete tool — remove a workspace file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.text import normalize_lock_key
from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.file_support import (
    FileReadState,
    invalidate_read_cache_for_path,
)
from src.tools.builtin.file_io.file_tool_base import WorkspaceBoundFileTool, WorkspaceBoundFileToolInvocation
from src.tools.builtin.file_io.trash import (
    move_to_trash,
    prune_expired_trash,
    should_use_trash,
)


class DeleteToolInvocation(WorkspaceBoundFileToolInvocation):
    """Invocation for the delete tool."""

    required_params = ("path",)
    normalize_read_states = True

    def __init__(
        self,
        params: dict[str, Any],
        read_state_store: dict[str, FileReadState] | None = None,
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        super().__init__(params, read_state_store, workspace_root, vfs_resolver)
        self.path = str(params["path"])

    def get_description(self) -> str:
        return f"删除 {self.path}"

    async def execute(self, signal=None) -> ToolResult:
        baseline_error = self._approval_baseline_error()
        if baseline_error is not None:
            return baseline_error
        path, path_error = self._resolve_delete_path()
        if path_error:
            return ToolResult.error(path_error)

        if path.is_dir():
            return ToolResult.error(f"目标是一个目录，本工具仅支持删除文件: {path}")
        if not path.exists():
            return ToolResult.error(f"文件不存在，无需删除: {path}")

        if should_use_trash(path, self._workspace_root):
            # 惰性 TTL 清理：每次入站前顺手清掉超期条目
            prune_expired_trash(self._workspace_root)
            try:
                trash_path = move_to_trash(path, self._workspace_root)
            except OSError as exc:
                return ToolResult.error(f"移入回收站失败，文件保留在原位: {exc}")

            self._clear_read_state(path)
            invalidate_read_cache_for_path(path)

            return ToolResult.success(
                content=f"已移入回收站: {path}",
                metadata={
                    "path": str(path),
                    "operation": "delete",
                    "trash_path": str(trash_path),
                },
            )

        # 工作区之外、宝箱 open/、回收站自身：不进回收站，审批通过后直接永久删除
        try:
            from src.vault.guard import mutate_vault_open_tree

            mutate_vault_open_tree(path, path.unlink)
        except PermissionError:
            return ToolResult.error(f"权限不足，无法删除文件: {path}")
        except OSError as exc:
            return ToolResult.error(f"删除文件失败: {exc}")
        except Exception as exc:
            # VaultLockedError during seal — surface as delete failure, no wipe race
            return ToolResult.error(f"删除文件失败: {exc}")

        self._clear_read_state(path)
        invalidate_read_cache_for_path(path)

        return ToolResult.success(
            content=f"已永久删除文件（不进入回收站）: {path}",
            metadata={"path": str(path), "operation": "delete", "permanent": True},
        )

    def _resolve_delete_path(self) -> tuple[Path | None, str | None]:
        return self._resolve_file_path_or_error(self.path, "Delete", allow_missing=True, allow_non_file=True)

    def _clear_read_state(self, path: Path) -> None:
        if self._read_states is None:
            return
        self._read_states.pop(str(path), None)
        self._read_states.pop(str(path.resolve()), None)


class DeleteTool(WorkspaceBoundFileTool):
    """Delete a single file inside the workspace."""

    name = "delete"
    prompts_for_out_of_mount_write = True
    description = """删除单个文件，不支持目录。工作区内文件移至 .coara/trash/ 并保留 7 天；vault open/、回收站内及已挂载工作区外文件会永久删除，后者需审批。不存在则返回提示"""  # noqa: E501
    display_name = "DeleteFile"
    category = "filesystem"
    kind = ToolKind.DELETE
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要删除的绝对文件路径",
            },
        },
        "required": ["path"],
    }

    invocation_class = DeleteToolInvocation
    normalize_read_states = True

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        return normalize_lock_key(args.get("path"))
