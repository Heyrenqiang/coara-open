"""Build PDF documents from Markdown or HTML via pandoc."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pandoc_builder import (
    InputFormat,
    build_document_via_pandoc,
    detect_input_format,
    infer_pdf_options,
)


def build_pdf_document(
    output_path: Path,
    *,
    content: str,
    title: str | None = None,
    toc: bool | None = None,
    number_sections: bool | None = None,
    pdf_engine: str | None = None,
    from_format: InputFormat | None = None,
) -> dict[str, Any]:
    """Create a .pdf file using pandoc."""
    input_format = detect_input_format(content) if from_format is None else from_format

    inferred_toc, inferred_number = infer_pdf_options(content)
    use_toc = inferred_toc if toc is None else toc
    use_number = inferred_number if number_sections is None else number_sections

    meta = build_document_via_pandoc(
        output_path,
        content=content,
        from_format=input_format,
        to_format="pdf",
        title=title,
        toc=use_toc,
        number_sections=use_number,
        pdf_engine=pdf_engine,
    )
    return meta
