"""Edit tool — Cursor StrReplace-aligned exact string replacement."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from src.coara.tool_output import build_diff_display, count_diff_stats
from src.core.text import normalize_lock_key
from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.edit_logic import simulate_edit_replacement
from src.tools.builtin.file_io.file_support import (
    DEFAULT_MAX_READ_BYTES,
    FileFormatInfo,
    FileReadState,
    load_text_with_snapshot_fallback,
    read_state_key,
    refresh_snapshot_after_write,
    write_text_file,
)
from src.tools.builtin.file_io.file_tool_base import WorkspaceBoundFileTool, WorkspaceBoundFileToolInvocation
from src.tools.builtin.file_io.office_editors import DocumentEditError, get_office_editor


class EditToolInvocation(WorkspaceBoundFileToolInvocation):
    """Edit tool invocation."""

    required_params = ("path", "old_string", "new_string")
    normalize_read_states = True

    def __init__(
        self,
        params: dict[str, Any],
        read_state_store: dict[str, FileReadState],
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        normalized = dict(params)
        super().__init__(normalized, read_state_store, workspace_root, vfs_resolver)
        if "path" not in normalized:
            raise ValueError("Missing required parameter: path")
        self.path = str(normalized["path"])
        self.old_string = normalized["old_string"]
        self.new_string = normalized["new_string"]
        self.replace_all = bool(normalized.get("replace_all", False))

    def get_description(self) -> str:
        verb = "全部替换" if self.replace_all else "编辑"
        return f"{verb} {self.path}"

    def _resolve_edit_path(self) -> tuple[Path | None, str | None]:
        return self._resolve_file_path_or_error(self.path, "Edit")

    async def execute(self, signal=None) -> ToolResult:
        baseline_error = self._approval_baseline_error()
        if baseline_error is not None:
            return baseline_error
        path, path_error = self._resolve_edit_path()
        if path_error:
            return ToolResult.error(path_error)
        path = cast(Path, path)

        office_editor = get_office_editor(path.suffix)
        if office_editor is not None:
            return await self._perform_office_edit(path, office_editor)

        # edit 需要整体读入文件做精确替换：超过上限直接拒绝（对齐 read_text_file
        # 默认上限），避免大文件整体读入造成内存尖峰
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            return ToolResult.error(f"读取文件失败: {exc}")
        if file_size > DEFAULT_MAX_READ_BYTES:
            return ToolResult.error(
                f"文件过大（约 {file_size / 1_000_000:.0f}MB），edit 需要整体读入文件，"
                f"仅支持不超过 {DEFAULT_MAX_READ_BYTES // 1_000_000}MB 的文件: {path}\n"
                "建议改用 shell 工具流式处理（如 sed / PowerShell 分段替换），或先拆分文件后再编辑"
            )

        read_state = self._read_states.get(read_state_key(path)) if self._read_states is not None else None
        try:
            current_content, format_info = load_text_with_snapshot_fallback(
                path,
                read_state,
                tool_name="edit",
            )
        except UnicodeDecodeError:
            return ToolResult.error(f"无法编辑二进制文件: {path}")
        except Exception as exc:
            return ToolResult.error(f"读取文件失败: {exc}")

        return await self._perform_edit(path, current_content, format_info)

    async def _perform_edit(self, path: Path, content: str, format_info: FileFormatInfo) -> ToolResult:
        simulated = simulate_edit_replacement(
            content,
            self.old_string,
            self.new_string,
            replace_all=self.replace_all,
        )
        if not simulated.ok:
            if simulated.error == "未找到匹配文本":
                from src.tools.builtin.file_io.edit_logic import _match_failure_context

                context = _match_failure_context(content, self.old_string)
                return ToolResult.error(f"未找到要替换的文本，{context}")
            return ToolResult.error(simulated.error)

        new_content = simulated.new_content
        matches = simulated.matches

        try:
            write_text_file(path, new_content, format_info)
        except Exception as exc:
            return ToolResult.error(f"写入文件失败: {exc}")

        refresh_snapshot_after_write(
            self._read_states,
            path,
            content=new_content,
            format_info=format_info,
        )

        display_blocks = await build_diff_display(str(path), content, new_content)
        added, removed = count_diff_stats(display_blocks)
        action = "已全部替换" if self.replace_all else "已替换"
        return ToolResult.success(
            content=f"已编辑 {path}：{action}（{matches} 处匹配）",
            metadata={
                "path": str(path),
                "replace_all": self.replace_all,
                "matches_found": matches,
                "lines_added": added,
                "lines_removed": removed,
            },
            display=display_blocks,
        )

    async def _perform_office_edit(self, path: Path, office_editor) -> ToolResult:
        try:
            result = office_editor(
                path,
                self.old_string,
                self.new_string,
                replace_all=self.replace_all,
            )
        except DocumentEditError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:
            return ToolResult.error(f"编辑 Office 文档失败: {exc}")

        matches = result.matches
        if matches == 0:
            return ToolResult.error(
                "未找到要替换的文本（Office 文档文本可能被拆成多个格式片段），请重新读取该文件核对原文。"
            )

        # Office 文档内容已改变，使缓存的文本快照失效。
        from src.tools.builtin.file_io.file_support import invalidate_read_cache_for_path

        invalidate_read_cache_for_path(path)

        # Diff from changed units recorded during the same open (no second parse).
        display_blocks = await build_diff_display(str(path), result.old_text, result.new_text)
        added, removed = count_diff_stats(display_blocks)
        action = "已全部替换" if self.replace_all else "已替换"
        return ToolResult.success(
            content=f"已编辑 {path}：{action}（{matches} 处匹配）",
            metadata={
                "path": str(path),
                "replace_all": self.replace_all,
                "matches_found": matches,
                "format": path.suffix.lower().lstrip("."),
                "lines_added": added,
                "lines_removed": removed,
            },
            display=display_blocks,
        )


class EditTool(WorkspaceBoundFileTool):
    """Performs exact string replacements in files."""

    name = "edit"
    description = """精确替换文本或 Office 文档内容；不支持 PDF。old_string 应唯一；失败时会尝试引号、空白和 Unicode 标点归一。多处匹配时先扩大 old_string 的上下文再替换；全部替换用 replace_all=true。Office 替换在段落/单元格/文本框内进行并保留格式"""  # noqa: E501
    display_name = "StrReplace"
    category = "filesystem"
    kind = ToolKind.EDIT
    prompts_for_out_of_mount_write = True

    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要编辑的绝对路径",
            },
            "old_string": {
                "type": "string",
                "description": "要替换的确切原文",
            },
            "new_string": {
                "type": "string",
                "description": "替换文本，必须不同于 old_string",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换全部匹配；默认 false，此时 old_string 必须唯一",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    invocation_class = EditToolInvocation
    normalize_read_states = True

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        return normalize_lock_key(args.get("path"))
