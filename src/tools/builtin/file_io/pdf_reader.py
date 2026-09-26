"""Extract text from PDF documents for the read tool."""

from __future__ import annotations

from pathlib import Path

from .office_errors import DocumentReadError


def _require_fitz():
    try:
        import fitz

        return fitz
    except ImportError as exc:  # pragma: no cover - exercised via missing-dep test
        raise DocumentReadError("未安装 PyMuPDF。请运行: pip install pymupdf") from exc


def read_pdf_document(path: Path) -> tuple[str, dict]:
    """Extract text page by page; pages separated by ``===== PAGE N =====`` markers.

    Returns (text, meta). Raises DocumentReadError for unreadable / textless
    (scanned) PDFs — the caller turns that into a ToolResult error.
    """
    fitz = _require_fitz()
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise DocumentReadError(f"PDF 打开失败: {exc}") from exc
    try:
        parts: list[str] = []
        for i, page in enumerate(doc):
            parts.append(f"\n===== PAGE {i + 1} =====\n")
            parts.append(page.get_text("text"))
        text = "".join(parts).strip()
        if not text:
            raise DocumentReadError("PDF 未提取到文本（可能是扫描件，需要 OCR）")
        return text, {"format": "pdf", "pages": len(doc)}
    finally:
        doc.close()
