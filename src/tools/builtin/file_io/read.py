"""Read tool — Cursor-aligned file reading."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from src.core.read_format import (
    count_text_lines,
    format_read_text_for_llm,
    read_slice_start_line,
    resolve_file_read_limit,
)
from src.core.text import slice_text_by_lines
from src.core.tool_base import ToolKind, ToolResult
from src.runtime.spill_read import read_spill_lines
from src.tools.builtin.file_io.file_support import (
    FileFormatInfo,
    FileReadState,
    current_file_size,
    current_timestamp,
    is_snapshot_current,
    log_file_cache_event,
    read_state_key,
    read_text_file,
    update_read_state,
)
from src.tools.builtin.file_io.file_tool_base import WorkspaceBoundFileTool, WorkspaceBoundFileToolInvocation
from src.tools.builtin.file_io.office_errors import DocumentReadError
from src.tools.cache import tool_cache


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


class ReadToolInvocation(WorkspaceBoundFileToolInvocation):
    """Invocation for the read tool."""

    required_params = ()
    path_read_only = True

    def __init__(
        self,
        params: dict[str, Any],
        read_state_store: dict[str, FileReadState] | None = None,
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
        parent_coara: Any | None = None,
    ):
        super().__init__(params, read_state_store, workspace_root, vfs_resolver)
        self.ref = str(params.get("ref") or "").strip()
        self.path = str(params.get("path") or "").strip()
        if not self.ref and not self.path:
            raise ValueError("Missing required parameter: path or ref")
        self.offset = _optional_int(params.get("offset"))
        self.limit = _optional_int(params.get("limit"))
        self._parent = parent_coara

    def get_description(self) -> str:
        if self.ref:
            return f"读取溢出 {self.ref}"
        return f"读取 {self.path}"

    def _slice(self, content: str) -> tuple[str, bool, int | None]:
        """Return (sliced_text, is_partial, effective_auto_limit)."""
        total_lines = count_text_lines(content)
        effective_limit = resolve_file_read_limit(
            offset=self.offset,
            limit=self.limit,
            total_lines=total_lines,
        )
        sliced = slice_text_by_lines(content, self.offset, effective_limit)
        auto_limited = self.limit is None and self.offset is None and effective_limit is not None
        is_partial = self.offset is not None or self.limit is not None or auto_limited or len(sliced) < len(content)
        return sliced, is_partial, effective_limit if auto_limited else None

    def _format_llm_content(self, sliced: str, full_content: str) -> str:
        total_lines = count_text_lines(full_content)
        start_line = read_slice_start_line(total_lines, self.offset)
        return format_read_text_for_llm(sliced, start_line=start_line)

    def _append_auto_limit_note(self, llm_text: str, *, total_lines: int, effective_limit: int) -> str:
        return f"{llm_text}\n\n... [仅显示前 {effective_limit}/{total_lines} 行]"

    async def _execute_ref_read(self) -> ToolResult:
        if self._parent is None:
            return ToolResult.error("read(ref=...) 需要绑定的 Coara 会话上下文")
        from src.core.config import config_manager
        from src.runtime.tool_output_store import ToolOutputStore

        coara_home = config_manager.config.coara_home if config_manager._config else None
        store = ToolOutputStore(
            workspace_dir=self._parent.workspace_dir,
            session_id=self._parent.session_id,
            coara_home=coara_home,
        )
        try:
            record = store.load(self.ref)
        except FileNotFoundError as exc:
            return ToolResult.error(str(exc))

        sliced, total_lines, effective_limit, is_partial = read_spill_lines(
            record.content,
            offset=self.offset,
            limit=self.limit,
        )
        auto_limited = self.limit is None and effective_limit is not None
        llm_text = self._format_llm_content(sliced, record.content)
        if auto_limited:
            llm_text += f"\n\n... [仅显示前 {effective_limit}/{total_lines} 行]"
        return ToolResult.success(
            content=llm_text,
            metadata={
                "ref": record.ref,
                "from_spill_ref": True,
                "tool_name": record.tool_name,
                "tool_call_id": record.tool_call_id,
                "offset": self.offset,
                "limit": effective_limit if effective_limit is not None else self.limit,
                "total_lines": total_lines,
                "total_length": len(record.content),
                "is_partial": is_partial,
                "auto_limited": auto_limited,
            },
        )

    async def _execute_image_read(self, path: Path) -> ToolResult:
        from src.utils.image_processor import (
            create_image_metadata_text,
            is_image_file,
            process_image_for_api,
        )
        from src.utils.multimodal_content import image_block_from_base64

        if not is_image_file(path):
            return ToolResult.error(f"不支持的图片格式: {path}")

        if self.offset is not None or self.limit is not None:
            return ToolResult.error("图片文件不支持 offset 或 limit 参数")

        cache_params = {
            "path": str(path),
            "format": "image",
            "mtime_ns": current_timestamp(path),
            "file_size": current_file_size(path),
        }
        cached_result = tool_cache.get("read", cache_params)
        if cached_result is not None:
            log_file_cache_event("read_cache_hit", path, format="image")
            return cached_result

        try:
            b64, media_type, meta = process_image_for_api(path, detail="high")
            image_block = image_block_from_base64(b64, media_type)
        except Exception as exc:
            return ToolResult.error(f"读取图片失败: {exc}")

        display_w = meta.get("display_width")
        display_h = meta.get("display_height")
        if display_w and display_h:
            summary = create_image_metadata_text(
                meta.get("original_width", display_w),
                meta.get("original_height", display_h),
                display_w,
                display_h,
            )
        else:
            summary = f"Image: {path.name}"
        summary = f"{summary}\nPath: {path}"

        result = ToolResult.success(
            content=[
                {"type": "text", "text": summary},
                image_block,
            ],
            metadata={
                **meta,
                "path": str(path),
                "format": "image",
                "media_type": media_type,
            },
        )
        tool_cache.set("read", cache_params, result)
        log_file_cache_event("read_cache_store", path, format="image")
        return result

    async def _execute_office_read(self, path: Path) -> ToolResult:
        """Read Office/PDF documents (.docx/.xlsx/.pptx/.pdf) as Markdown-ish text."""
        from src.tools.builtin.file_io.excel_reader import read_excel_workbook
        from src.tools.builtin.file_io.pdf_reader import read_pdf_document
        from src.tools.builtin.file_io.pptx_reader import read_pptx_presentation
        from src.tools.builtin.file_io.word_reader import read_word_document

        readers = {
            ".docx": read_word_document,
            ".xlsx": read_excel_workbook,
            ".pptx": read_pptx_presentation,
            ".pdf": read_pdf_document,
        }
        reader = readers.get(path.suffix.lower())
        if reader is None:
            return ToolResult.error(f"不支持的 Office 文件格式: {path}")

        cache_params = {
            "path": str(path),
            "format": "office",
            "mtime_ns": current_timestamp(path),
            "file_size": current_file_size(path),
        }
        cached_result = tool_cache.get("read", cache_params)
        if cached_result is not None:
            log_file_cache_event("read_cache_hit", path, format="office")
            return cached_result

        try:
            full_content, meta = reader(path)
        except DocumentReadError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:
            return ToolResult.error(f"读取 Office 文档失败: {exc}")

        display_text, is_partial, auto_limit = self._slice(full_content)
        llm_text = self._format_llm_content(display_text, full_content)
        total_lines = count_text_lines(full_content)
        if auto_limit is not None:
            llm_text = self._append_auto_limit_note(llm_text, total_lines=total_lines, effective_limit=auto_limit)
        elif is_partial and self.offset is None and self.limit is None:
            llm_text += f"\n\n... [仅显示部分内容，共 {total_lines} 行]"

        update_read_state(
            self._read_states,
            path,
            content=full_content,
            format_info=FileFormatInfo(),
        )

        result = ToolResult.success(
            content=llm_text,
            metadata={
                "path": str(path),
                "length": len(display_text),
                "total_length": len(full_content),
                "total_lines": total_lines,
                "is_partial": is_partial,
                "auto_limited": auto_limit is not None,
                "limit": auto_limit if auto_limit is not None else self.limit,
                **meta,
            },
        )
        tool_cache.set("read", cache_params, result)
        log_file_cache_event("read_cache_store", path, format="office", partial=is_partial)
        return result

    async def execute(self, signal=None) -> ToolResult:
        if self.ref:
            return await self._execute_ref_read()

        path, path_error = self._resolve_file_path_or_error(self.path, "Read")
        if path_error:
            return ToolResult.error(path_error)
        path = cast(Path, path)

        from src.utils.image_processor import is_image_file

        if is_image_file(path):
            return await self._execute_image_read(path)

        if path.suffix.lower() in {".docx", ".xlsx", ".pptx", ".pdf"}:
            return await self._execute_office_read(path)

        try:
            cache_params = {
                "path": str(path),
                "offset": self.offset,
                "limit": self.limit,
                "mtime_ns": current_timestamp(path),
                "file_size": current_file_size(path),
            }
            read_state = self._read_states.get(read_state_key(path)) if self._read_states is not None else None

            if read_state is not None and is_snapshot_current(path, read_state):
                cached_result = tool_cache.get("read", cache_params)
                if cached_result is not None:
                    log_file_cache_event("read_cache_hit", path, offset=self.offset, limit=self.limit)
                    return cached_result
                full_content = read_state.content
                format_info = read_state.format_info
                log_file_cache_event("snapshot_hit", path, tool="read", offset=self.offset, limit=self.limit)
            else:
                log_file_cache_event("disk_read_fallback", path, tool="read", reason="missing_or_stale_snapshot")
                full_content, format_info = read_text_file(path)
        except UnicodeDecodeError:
            return ToolResult.error(f"无法读取二进制文件: {path}")
        except Exception as exc:
            return ToolResult.error(f"读取文件失败: {exc}")

        display_text, is_partial, auto_limit = self._slice(full_content)
        llm_text = self._format_llm_content(display_text, full_content)
        total_lines = count_text_lines(full_content)
        if auto_limit is not None:
            llm_text = self._append_auto_limit_note(llm_text, total_lines=total_lines, effective_limit=auto_limit)
        elif is_partial and self.offset is None and self.limit is None:
            llm_text += f"\n\n... [仅显示部分内容，共 {total_lines} 行]"

        update_read_state(
            self._read_states,
            path,
            content=full_content,
            format_info=format_info,
        )

        result = ToolResult.success(
            content=llm_text,
            metadata={
                "path": str(path),
                "length": len(display_text),
                "total_length": len(full_content),
                "total_lines": total_lines,
                "is_partial": is_partial,
                "auto_limited": auto_limit is not None,
                "offset": self.offset,
                "limit": auto_limit if auto_limit is not None else self.limit,
                "encoding": format_info.encoding,
                "line_ending": "CRLF" if format_info.line_ending == "\r\n" else "LF",
            },
        )
        tool_cache.set("read", cache_params, result)
        log_file_cache_event("read_cache_store", path, offset=self.offset, limit=self.limit, partial=is_partial)
        return result


class ReadTool(WorkspaceBoundFileTool):
    """Read a file from the local filesystem."""

    name = "read"
    description = """读取本地文件，支持文本、图片、Office 与 PDF；不支持的二进制文件会报错，改用 shell。文本按行返回并带行号前缀，不是原文；单行最多 2000 字符。默认最多 2000 行，可用 offset/limit 续读；其他工具的溢出结果可用 ref 读取"""  # noqa: E501
    display_name = "Read"
    category = "filesystem"
    kind = ToolKind.READ
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件绝对路径；与 ref 二选一，同时提供时 ref 优先；可读取工作区外文件",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号，从 1 开始；负数表示从末尾倒数",
            },
            "limit": {
                "type": "integer",
                "description": "读取行数；省略时，大文件默认返回前 2000 行",
            },
            "ref": {
                "type": "string",
                "description": "其他工具溢出结果的 ref；大存档未指定 limit 时返回前 500 行",
            },
        },
    }

    def __init__(
        self,
        read_state_store: dict[str, FileReadState] | None = None,
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
        parent_coara: Any | None = None,
    ):
        super().__init__(read_state_store, workspace_root, vfs_resolver)
        self._parent_coara = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ReadToolInvocation:
        return ReadToolInvocation(
            params,
            self._read_states,
            self._workspace_root,
            self._vfs_resolver,
            parent_coara=self._parent_coara,
        )

    invocation_class = ReadToolInvocation
