"""Write tool — Cursor-aligned file writing with Office/PDF extensions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.core.text import normalize_lock_key
from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.excel_builder import build_excel_workbook
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
from src.tools.builtin.file_io.office_errors import DocumentBuildError
from src.tools.builtin.file_io.pandoc_builder import (
    InputFormat,
    build_document_via_pandoc,
    detect_input_format,
    needs_rich_render,
    pandoc_available,
    pdf_engine_available,
)
from src.tools.builtin.file_io.pdf_builder import build_pdf_document
from src.tools.builtin.file_io.word_builder import build_word_document

_DOCX = ".docx"
_XLSX = ".xlsx"
_PDF = ".pdf"
_WORD_STYLE = "report"
_EXCEL_STYLE = "generic"

_OFFICE_SCHEMA_EXTENSIONS = {
    "title": {"type": "string", "description": "docx/PDF 可选标题，不替代 contents"},
    "template_path": {
        "type": "string",
        "description": "docx 模板绝对路径，通常无需使用",
    },
    "sheets": {
        "type": "array",
        "description": (
            "xlsx 工作表列表；每项含 name、headers、rows，title 可选"
        ),
        "items": {"type": "object"},
    },
}


def _resolve_write_contents(params: dict[str, Any]) -> str:
    """Accept Cursor-aligned `contents` and common alias `content`."""
    if "contents" in params:
        return str(params.get("contents") or "")
    if "content" in params:
        return str(params.get("content") or "")
    return ""


def _docx_basic_available() -> bool:
    try:
        import docx  # noqa: F401

        return True
    except ImportError:
        return False


def _build_word_docx(
    path: Path,
    content: str,
    *,
    title: str | None,
    template: Path | None = None,
) -> dict[str, Any]:
    return build_word_document(
        path,
        content=content,
        style=_WORD_STYLE,
        title=title,
        template_path=template,
    )


def _docx_tool_result(
    message: str,
    meta: dict[str, Any],
    *,
    engine: str,
    degraded: bool = False,
    from_format: InputFormat | None = None,
) -> ToolResult:
    payload: dict[str, Any] = {**meta, "format": "docx", "engine": engine, "degraded": degraded}
    if from_format is not None:
        payload["from_format"] = from_format
    return ToolResult.success(message, metadata=payload)


class WriteToolInvocation(WorkspaceBoundFileToolInvocation):
    """Invocation for the unified write tool."""

    normalize_read_states = True

    def __init__(
        self,
        params: dict[str, Any],
        read_state_store: dict[str, FileReadState] | None = None,
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        super().__init__(params, read_state_store, workspace_root, vfs_resolver)
        if "path" not in params:
            raise ValueError("缺少必填参数: path")
        self.path = str(params["path"])
        self.suffix = Path(self.path).suffix.lower()
        self.contents = _resolve_write_contents(params)
        self.title = str(params.get("title", "") or "").strip()
        self.template_path = str(params.get("template_path", "") or "").strip()
        raw_sheets = params.get("sheets")
        self.sheets = raw_sheets if isinstance(raw_sheets, list) else None
        self._validate_params(params)

    def _validate_params(self, params: dict[str, Any]) -> None:
        if self.suffix == _XLSX:
            if not self.sheets:
                raise ValueError("生成 .xlsx 必须提供非空的 sheets 参数。")
            return
        if self.suffix in (_DOCX, _PDF) and not self.contents.strip():
            raise ValueError(
                f"生成 {self.suffix} 必须提供非空的 contents（支持 Markdown 或 HTML）。可选 title 不能替代正文。"
            )
        if self.suffix not in (_DOCX, _XLSX, _PDF) and "contents" not in params and "content" not in params:
            raise ValueError("缺少必填参数: contents")

    def get_description(self) -> str:
        labels = {_DOCX: "Word", _XLSX: "Excel", _PDF: "PDF"}
        label = labels.get(self.suffix)
        return f"写入{label} {self.path}" if label else f"写入 {self.path}"

    async def execute(self, signal=None) -> ToolResult:
        baseline_error = self._approval_baseline_error()
        if baseline_error is not None:
            return baseline_error
        handlers = {
            _DOCX: self._execute_docx,
            _XLSX: self._execute_xlsx,
            _PDF: self._execute_pdf,
        }
        handler = handlers.get(self.suffix, self._execute_text)
        return await handler()

    def _resolve_write_path(self, required_suffix: str | None = None) -> tuple[Path | None, ToolResult | None]:
        path, path_error = self._resolve_file_path_or_error(
            self.path, "Write", allow_missing=True, required_suffix=required_suffix
        )
        if path_error:
            return None, ToolResult.error(path_error)
        return path, None

    def _resolve_template_path(self) -> tuple[Path | None, ToolResult | None]:
        if not self.template_path:
            return None, None
        tpl_path, tpl_error = self._resolve_file_path_or_error(self.template_path, "Write")
        if tpl_error:
            return None, ToolResult.error(tpl_error)
        return tpl_path, None

    async def _execute_docx(self) -> ToolResult:
        path, error_result = self._resolve_write_path(required_suffix=_DOCX)
        if error_result is not None:
            return error_result
        assert path is not None

        template, tpl_error = self._resolve_template_path()
        if tpl_error is not None:
            return tpl_error

        if template is not None:
            try:
                meta = _build_word_docx(path, self.contents, title=self.title or None, template=template)
            except DocumentBuildError as exc:
                return ToolResult.error(str(exc))
            return _docx_tool_result(
                f"已写入 Word 文档: {meta['path']}",
                meta,
                engine="python-docx",
            )

        rich = needs_rich_render(self.contents)
        from_format = detect_input_format(self.contents)

        if rich and pandoc_available():
            try:
                meta = await asyncio.to_thread(
                    build_document_via_pandoc,
                    path,
                    content=self.contents,
                    from_format=from_format,
                    to_format="docx",
                    title=self.title or None,
                )
                return _docx_tool_result(
                    f"已写入 Word 文档: {meta['path']}",
                    meta,
                    engine="pandoc",
                    from_format=from_format,
                )
            except DocumentBuildError:
                try:
                    meta = _build_word_docx(path, self.contents, title=self.title or None)
                except DocumentBuildError as fallback_exc:
                    return ToolResult.error(f"文档生成失败: {fallback_exc}")
                return _docx_tool_result(
                    f"已写入 Word 文档（基础排版，pandoc 异常已降级）: {meta['path']}",
                    meta,
                    engine="python-docx",
                    degraded=True,
                    from_format=from_format,
                )

        try:
            meta = _build_word_docx(path, self.contents, title=self.title or None)
        except DocumentBuildError as exc:
            return ToolResult.error(str(exc))

        note = ""
        degraded = False
        if rich and not pandoc_available():
            note = "；建议安装 pandoc 以支持表格/颜色等富文本"
            degraded = True
        return _docx_tool_result(
            f"已写入 Word 文档: {meta['path']}{note}",
            meta,
            engine="python-docx",
            degraded=degraded,
            from_format=from_format,
        )

    async def _execute_xlsx(self) -> ToolResult:
        path, error_result = self._resolve_write_path(required_suffix=_XLSX)
        if error_result is not None:
            return error_result
        assert path is not None and self.sheets is not None

        try:
            meta = build_excel_workbook(path, sheets=self.sheets, style=_EXCEL_STYLE)
        except DocumentBuildError as exc:
            return ToolResult.error(str(exc))

        return ToolResult.success(
            f"已写入 Excel 工作簿: {meta['path']}",
            metadata={**meta, "format": "xlsx", "engine": "openpyxl"},
        )

    async def _execute_pdf(self) -> ToolResult:
        path, error_result = self._resolve_write_path(required_suffix=_PDF)
        if error_result is not None:
            return error_result
        assert path is not None

        if not pandoc_available():
            return ToolResult.error("生成 PDF 必须安装 pandoc。")

        try:
            meta = await asyncio.to_thread(build_pdf_document, path, content=self.contents, title=self.title or None)
        except DocumentBuildError as exc:
            return ToolResult.error(str(exc))

        return ToolResult.success(
            f"已写入 PDF: {meta['path']}",
            metadata={**meta, "format": "pdf", "degraded": False},
        )

    async def _execute_text(self) -> ToolResult:
        path, error_result = self._resolve_write_path()
        if error_result is not None:
            return error_result
        assert path is not None

        file_exists = path.exists()
        format_info = FileFormatInfo()

        if file_exists:
            # write 覆盖前要整体读入原文件（保留编码/换行风格、拦截二进制）：超过上限
            # 直接拒绝（对齐 edit 与 read_text_file 默认上限），避免 snapshot 磁盘回退
            # 把超大文件整体读入内存
            try:
                file_size = path.stat().st_size
            except OSError as exc:
                return ToolResult.error(f"读取文件失败: {exc}")
            if file_size > DEFAULT_MAX_READ_BYTES:
                return ToolResult.error(
                    f"文件过大（约 {file_size / 1_000_000:.0f}MB），write 覆盖已存在文件需整体读入原文件，"
                    f"仅支持不超过 {DEFAULT_MAX_READ_BYTES // 1_000_000}MB 的文件: {path}\n"
                    "建议改用 shell 工具流式处理，或先拆分文件后再写入"
                )
            read_state = self._read_states.get(read_state_key(path)) if self._read_states is not None else None
            try:
                _, format_info = load_text_with_snapshot_fallback(path, read_state, tool_name="write")
            except UnicodeDecodeError:
                return ToolResult.error(
                    f"无法写入二进制文件: {path}。Office 文件请使用 write(path=*.docx) 或 write(path=*.xlsx)。"
                )
            except Exception as exc:
                return ToolResult.error(f"写入前读取文件失败: {exc}")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_file(path, self.contents, format_info)
        except Exception as exc:
            return ToolResult.error(f"写入文件失败: {exc}")

        refresh_snapshot_after_write(self._read_states, path, content=self.contents, format_info=format_info)

        operation = "覆盖" if file_exists else "创建"

        return ToolResult.success(
            content=f"已向 {path} 写入 {len(self.contents)} 个字符（{operation}）",
            metadata={
                "path": str(path),
                "bytes_written": len(self.contents.encode("utf-8")),
                "operation": operation,
                "format": "text",
            },
        )


class WriteTool(WorkspaceBoundFileTool):
    """Write a file to the local filesystem."""

    name = "write"
    prompts_for_out_of_mount_write = True
    description = """覆盖写入本地文件，自动创建父目录；path 必须为绝对路径。文本按原样写入；docx/PDF 接受 Markdown 或 HTML，PDF 依赖 pandoc，未装则报错；xlsx 使用 sheets，contents 可省略。contents 受本轮 token 预算限制，大文件应拆分写入或用 edit 追加"""
    display_name = "Write"
    category = "filesystem"
    kind = ToolKind.EDIT
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的绝对路径；后缀决定格式",
            },
            "contents": {
                "type": "string",
                "description": "完整覆盖内容；代码须完整；docx/PDF 支持 Markdown 或 HTML；xlsx 可改用 sheets",
            },
            **_OFFICE_SCHEMA_EXTENSIONS,
        },
        "required": ["path"],
    }

    invocation_class = WriteToolInvocation
    normalize_read_states = True

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        return normalize_lock_key(args.get("path"))

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "pandoc": pandoc_available(),
            "pdf": pandoc_available() and pdf_engine_available(),
            "docx_rich": pandoc_available(),
            "docx_basic": _docx_basic_available(),
        }
