"""Unified document builder via pandoc for Word/PDF generation."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from .office_errors import DocumentBuildError

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_H1_MD_RE = re.compile(r"^#\s+", re.MULTILINE)

_RICH_HTML_MARKERS = (
    "<table",
    "<div",
    "<h1",
    "<h2",
    "<h3",
    "<h4",
    "<h5",
    "<h6",
    "<ul",
    "<ol",
    "<p>",
    "<span style",
    "<td style",
    "<font",
    "background-color",
    "color:",
)

InputFormat = Literal["html", "markdown"]
OutputFormat = Literal["docx", "pdf"]


def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _ensure_pandoc() -> str:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise DocumentBuildError(
            "生成此格式需要安装 pandoc。\n"
            "  macOS:   brew install pandoc\n"
            "  Ubuntu:  sudo apt install pandoc\n"
            "  Windows: choco install pandoc  (https://pandoc.org/installing.html)\n"
            "PDF 还需 typst 或 TeX 引擎（如 MiKTeX xelatex）。"
        )
    return pandoc


def needs_rich_render(content: str) -> bool:
    """True when content likely needs pandoc (HTML tables, inline styles, etc.)."""
    stripped = content.lstrip()[:2000].lower()
    if stripped.startswith("<!doctype") or stripped.startswith("<html"):
        return True
    return any(marker in stripped for marker in _RICH_HTML_MARKERS)


def detect_input_format(content: str) -> InputFormat:
    """Infer whether pandoc should treat contents as HTML or Markdown."""
    if needs_rich_render(content):
        return "html"
    return "markdown"


def infer_pdf_options(content: str) -> tuple[bool, bool]:
    """Return (toc, number_sections) from content heuristics."""
    body = content.strip()
    if not body:
        return False, False
    h1_count = len(_H1_MD_RE.findall(body))
    if h1_count >= 3 or len(body) > 8000:
        return True, True
    return False, False


def default_pdf_engine(content: str) -> str | None:
    """Prefer typst (lightweight); fall back to xelatex for legacy TeX installs."""
    if shutil.which("typst"):
        return "typst"
    if _has_cjk(content) and shutil.which("xelatex"):
        return "xelatex"
    if shutil.which("pdflatex"):
        return "pdflatex"
    return None


def pdf_engine_available(content: str = "") -> bool:
    return default_pdf_engine(content) is not None


def _pdf_engine_args(*, content: str, pdf_engine: str | None) -> list[str]:
    if pdf_engine:
        args = ["--pdf-engine", pdf_engine]
        if pdf_engine == "xelatex" and _has_cjk(content):
            args.extend(["-V", "CJKmainfont=SimSun", "-V", "geometry:margin=2.5cm"])
        return args
    engine = default_pdf_engine(content)
    if engine is None:
        return []
    if engine == "xelatex" and _has_cjk(content):
        return [
            "--pdf-engine",
            "xelatex",
            "-V",
            "CJKmainfont=SimSun",
            "-V",
            "geometry:margin=2.5cm",
        ]
    return ["--pdf-engine", engine]


def _prepare_content(content: str, *, from_format: InputFormat, title: str | None) -> str:
    body = content.strip()
    if not body:
        raise DocumentBuildError("文档内容不能为空。")
    if not title:
        return f"{body}\n"
    if from_format == "html":
        lower = body[:500].lower()
        if "<h1" not in lower:
            return f"<h1>{title}</h1>\n{body}\n"
        return f"{body}\n"
    if not body.lstrip().startswith("#"):
        return f"# {title}\n\n{body}\n"
    return f"{body}\n"


def build_document_via_pandoc(
    output_path: Path,
    *,
    content: str,
    from_format: InputFormat,
    to_format: OutputFormat,
    title: str | None = None,
    toc: bool = False,
    number_sections: bool = False,
    pdf_engine: str | None = None,
    reference_doc: Path | None = None,
) -> dict[str, Any]:
    """Convert HTML or Markdown to .docx or .pdf via pandoc."""
    pandoc = _ensure_pandoc()
    prepared = _prepare_content(content, from_format=from_format, title=title)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = [
        pandoc,
        "-f",
        from_format,
        "-t",
        to_format,
        "-o",
        str(output_path),
    ]
    if title and to_format == "pdf":
        args.extend(["--metadata", f"title={title}"])
    if reference_doc is not None:
        args.extend(["--reference-doc", str(reference_doc)])
    if to_format == "pdf":
        if toc:
            args.append("--toc")
        if number_sections:
            args.append("--number-sections")
        engine_args = _pdf_engine_args(content=prepared, pdf_engine=pdf_engine)
        if not engine_args and pdf_engine is None and default_pdf_engine(prepared) is None:
            raise DocumentBuildError("生成 PDF 需要安装 typst 或 xelatex/pdflatex 引擎。")
        args.extend(engine_args)
    args.append("-")

    try:
        completed = subprocess.run(
            args,
            input=prepared.encode("utf-8"),
            capture_output=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocumentBuildError("pandoc 转换超时（180s）。") from exc
    except OSError as exc:
        raise DocumentBuildError(f"pandoc 执行失败: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        hint = ""
        if to_format == "pdf" and _has_cjk(prepared) and "xelatex" in stderr.lower():
            hint = "\n提示：可安装 typst（推荐）或 MiKTeX/TeX Live 以支持中文 PDF。"
        raise DocumentBuildError(f"pandoc 转换失败: {stderr or '(无错误输出)'}{hint}")

    if not output_path.is_file():
        raise DocumentBuildError(f"pandoc 未生成输出文件: {output_path}")

    resolved_engine = pdf_engine or (default_pdf_engine(prepared) if to_format == "pdf" else None)
    return {
        "path": str(output_path.resolve()),
        "bytes": output_path.stat().st_size,
        "engine": "pandoc",
        "from_format": from_format,
        "toc": toc,
        "number_sections": number_sections,
        "pdf_engine": resolved_engine or "default",
    }
