"""In-place text replacement for Office documents (.docx/.xlsx/.pptx)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .office_support import require_pkg

# Cap units fed to CLI/Web diff so replace_all on huge sheets stays cheap.
# Match count is still exact; only display text is truncated.
_MAX_DIFF_UNITS = 100


class DocumentEditError(Exception):
    """Raised when an Office document edit fails."""


@dataclass(slots=True)
class OfficeEditResult:
    """Outcome of an in-place Office edit, including text for terminal diff."""

    matches: int
    old_text: str = ""
    new_text: str = ""


def _save_office_atomic(save_fn: Callable[[str], None], path: Path) -> None:
    """Save via a unique sibling temp file + replace; the original stays intact on failure."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        save_fn(str(temp_path))
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _require_docx() -> None:
    require_pkg("docx", DocumentEditError, "未安装 python-docx。请运行: pip install python-docx")


def _require_openpyxl() -> None:
    require_pkg("openpyxl", DocumentEditError, "未安装 openpyxl。请运行: pip install openpyxl")


def _require_pptx() -> None:
    try:
        from pptx import Presentation  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise DocumentEditError("未安装 python-pptx。请运行: pip install python-pptx") from exc


def _replace_in_text(text: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    """Simple text replacement helper. Returns (new_text, match_count)."""
    if old not in text:
        return text, 0
    if replace_all:
        count = text.count(old)
        return text.replace(old, new), count
    return text.replace(old, new, 1), 1


def _replace_in_runs(runs: list, old: str, new: str, replace_all: bool) -> int:
    """Replace text across a sequence of runs, one run at a time.

    This preserves formatting for in-run replacements. Cross-run matches are not
    supported — they are rare for LLM-style edits and would require run merging.
    """
    total = 0
    for run in runs:
        if old not in run.text:
            continue
        if replace_all:
            count = run.text.count(old)
            run.text = run.text.replace(old, new)
            total += count
        else:
            run.text = run.text.replace(old, new, 1)
            total += 1
            break
    return total


def _record_unit(old_parts: list[str], new_parts: list[str], before: str, after: str) -> None:
    """Keep before/after of a changed unit for diff, capped for performance."""
    if before == after or len(old_parts) >= _MAX_DIFF_UNITS:
        return
    old_parts.append(before)
    new_parts.append(after)


def _finish_result(
    matches: int,
    old_parts: list[str],
    new_parts: list[str],
    *,
    old_string: str,
    new_string: str,
) -> OfficeEditResult:
    """Build edit result; fall back to the replacement strings if no units recorded."""
    if matches <= 0:
        return OfficeEditResult(matches=0)
    if old_parts and new_parts:
        return OfficeEditResult(
            matches=matches,
            old_text="\n".join(old_parts),
            new_text="\n".join(new_parts),
        )
    # Cross-run / edge cases: still show the intended substitution.
    return OfficeEditResult(matches=matches, old_text=old_string, new_text=new_string)


def edit_word_document(
    path: Path,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> OfficeEditResult:
    """Replace ``old_string`` with ``new_string`` inside a .docx file.

    Replacement happens at the paragraph level (and inside table cells) while
    preserving surrounding formatting. Returns matches plus changed-unit text
    for terminal diff (no second document open).

    Raises:
        DocumentEditError: if python-docx is missing or the file cannot be saved.
    """
    _require_docx()
    from docx import Document

    try:
        document = Document(str(path))
    except Exception as exc:
        raise DocumentEditError(f"无法打开 Word 文档: {path}（{exc}）") from exc

    total = 0
    old_parts: list[str] = []
    new_parts: list[str] = []

    for para in document.paragraphs:
        if old_string not in para.text:
            continue
        before = para.text
        count = _replace_in_runs(para.runs, old_string, new_string, replace_all)
        if count:
            _record_unit(old_parts, new_parts, before, para.text)
            total += count
            if not replace_all:
                break

    if total == 0 or replace_all:
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        if old_string not in para.text:
                            continue
                        before = para.text
                        count = _replace_in_runs(para.runs, old_string, new_string, replace_all)
                        if not count:
                            continue
                        _record_unit(old_parts, new_parts, before, para.text)
                        total += count
                        if not replace_all:
                            break
                    if total > 0 and not replace_all:
                        break
                if total > 0 and not replace_all:
                    break
            if total > 0 and not replace_all:
                break

    if total == 0:
        return OfficeEditResult(matches=0)

    try:
        _save_office_atomic(document.save, path)
    except Exception as exc:
        raise DocumentEditError(f"保存 Word 文档失败: {path}（{exc}）") from exc

    return _finish_result(total, old_parts, new_parts, old_string=old_string, new_string=new_string)


def edit_excel_workbook(
    path: Path,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> OfficeEditResult:
    """Replace ``old_string`` with ``new_string`` inside a .xlsx file.

    Only string cell values are considered. Returns matches plus changed-cell text
    for terminal diff (no second workbook open).

    Raises:
        DocumentEditError: if openpyxl is missing or the file cannot be saved.
    """
    _require_openpyxl()
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(filename=str(path))
    except Exception as exc:
        raise DocumentEditError(f"无法打开 Excel 工作簿: {path}（{exc}）") from exc

    total = 0
    old_parts: list[str] = []
    new_parts: list[str] = []
    for ws in workbook.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str):
                    continue
                if old_string not in value:
                    continue
                new_value, count = _replace_in_text(value, old_string, new_string, replace_all)
                if not count:
                    continue
                cell.value = new_value
                _record_unit(old_parts, new_parts, value, new_value)
                total += count
                if not replace_all:
                    break
            if total > 0 and not replace_all:
                break
        if total > 0 and not replace_all:
            break

    if total == 0:
        return OfficeEditResult(matches=0)

    try:
        _save_office_atomic(workbook.save, path)
    except Exception as exc:
        raise DocumentEditError(f"保存 Excel 工作簿失败: {path}（{exc}）") from exc

    return _finish_result(total, old_parts, new_parts, old_string=old_string, new_string=new_string)


def edit_pptx_presentation(
    path: Path,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> OfficeEditResult:
    """Replace ``old_string`` with ``new_string`` inside a .pptx file.

    Replacement happens inside text frames and table cells. Returns matches plus
    changed-unit text for terminal diff (no second presentation open).

    Raises:
        DocumentEditError: if python-pptx is missing or the file cannot be saved.
    """
    _require_pptx()
    from pptx import Presentation

    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        raise DocumentEditError(f"无法打开 PowerPoint 演示文稿: {path}（{exc}）") from exc

    total = 0
    old_parts: list[str] = []
    new_parts: list[str] = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                table = shape.table
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in cell.text_frame.paragraphs:
                            if old_string not in paragraph.text:
                                continue
                            before = paragraph.text
                            count = _replace_in_runs(paragraph.runs, old_string, new_string, replace_all)
                            if not count:
                                continue
                            _record_unit(old_parts, new_parts, before, paragraph.text)
                            total += count
                            if not replace_all:
                                break
                        if total > 0 and not replace_all:
                            break
                    if total > 0 and not replace_all:
                        break
            elif getattr(shape, "has_text_frame", False):
                text_frame = shape.text_frame
                for paragraph in text_frame.paragraphs:
                    if old_string not in paragraph.text:
                        continue
                    before = paragraph.text
                    count = _replace_in_runs(paragraph.runs, old_string, new_string, replace_all)
                    if not count:
                        continue
                    _record_unit(old_parts, new_parts, before, paragraph.text)
                    total += count
                    if not replace_all:
                        break
            if total > 0 and not replace_all:
                break
        if total > 0 and not replace_all:
            break

    if total == 0:
        return OfficeEditResult(matches=0)

    try:
        presentation.save(str(path))
    except Exception as exc:
        raise DocumentEditError(f"保存 PowerPoint 演示文稿失败: {path}（{exc}）") from exc

    return _finish_result(total, old_parts, new_parts, old_string=old_string, new_string=new_string)


def get_office_editor(suffix: str) -> Any | None:
    """Return the editor function for a given Office suffix, or None."""
    editors = {
        ".docx": edit_word_document,
        ".xlsx": edit_excel_workbook,
        ".pptx": edit_pptx_presentation,
    }
    return editors.get(suffix.lower())
