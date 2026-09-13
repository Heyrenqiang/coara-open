"""本机打印核心 — 枚举打印机与提交打印任务（Windows: SumatraPDF/MSPaint；Linux/macOS: CUPS）。

从 mcp-tools/printer_client.py 迁入（MCP 退役，能力转内置挂起工具）。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
        ".webp",
    }
)
_MAX_COPIES = 20
_PAGES_RE = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")
_WIN_STATUS = {1: "other", 2: "unknown", 3: "idle", 4: "printing", 5: "warmup", 6: "stopped", 7: "offline"}


def _run_command(args: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    from src.utils.win_proc import no_window_creationflags

    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=no_window_creationflags(),
    )


def _run_copies(copies: int, runner: Callable[[], None], *, label: str) -> None:
    for idx in range(1, copies + 1):
        try:
            runner()
        except RuntimeError as exc:
            raise RuntimeError(f"{label} 第 {idx}/{copies} 份失败: {exc}") from exc


def _format_summary(
    path: Path,
    printer: str,
    *,
    copies: int,
    pages: str = "",
    backend: str = "",
    extra: str = "",
) -> str:
    parts = [f"已提交打印: {path.name} → {printer}"]
    if copies > 1:
        parts.append(f"（{copies} 份）")
    if pages:
        parts.append(f"，页码 {pages}")
    if backend:
        parts.append(f"（{backend}）")
    if extra:
        parts.append(f"；{extra}")
    return "".join(parts)


def _resolve_file_path(file_path: str) -> Path:
    raw = (file_path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("file_path 不能为空")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"file_path 必须是绝对路径: {raw}")
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    ext = path.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(_SUPPORTED_EXTENSIONS))
        raise ValueError(f"不支持的文件类型 {ext!r}，支持: {supported}")
    return path


def _resolve_target_printer(printer_name: str, printers: list[dict[str, Any]]) -> str:
    if printer_name:
        target = printer_name.strip()
    else:
        target = next((str(p["name"]) for p in printers if p.get("is_default")), "")
    if not target:
        raise RuntimeError("未找到默认打印机，请先 list_printers 并指定 printer_name")
    known = {str(p["name"]) for p in printers}
    if target not in known:
        raise RuntimeError(f"打印机不存在: {target}")
    return target


def list_printers() -> list[dict[str, Any]]:
    """Return installed printers (name, is_default, status)."""
    system = platform.system()
    if system == "Windows":
        return _list_printers_windows()
    if system in {"Linux", "Darwin"}:
        return _list_printers_cups()
    raise RuntimeError(f"当前操作系统暂不支持打印: {system}")


def print_file(
    file_path: str,
    printer_name: str = "",
    copies: int = 1,
    pages: str = "",
) -> str:
    """Submit a local PDF or image file to the printer."""
    path = _resolve_file_path(file_path)
    printer = (printer_name or "").strip()
    if copies < 1 or copies > _MAX_COPIES:
        raise ValueError(f"copies 必须在 1–{_MAX_COPIES} 之间")
    pages = (pages or "").strip()
    if pages and not _PAGES_RE.match(pages):
        raise ValueError("pages 格式无效，示例: 1-3 或 1,3-5")
    if pages and path.suffix.lower() != ".pdf":
        raise ValueError("pages 参数仅适用于 PDF 文件")

    system = platform.system()
    if system == "Windows":
        return _print_file_windows(path, printer, copies, pages)
    if system in {"Linux", "Darwin"}:
        return _print_file_cups(path, printer, copies, pages)
    raise RuntimeError(f"当前操作系统暂不支持打印: {system}")


def _parse_page_numbers(pages: str) -> list[int]:
    numbers: list[int] = []
    for part in pages.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start, end = int(start_raw), int(end_raw)
            if start < 1 or end < start:
                raise ValueError(f"页码范围无效: {token}")
            numbers.extend(range(start, end + 1))
        else:
            page = int(token)
            if page < 1:
                raise ValueError(f"页码必须 >= 1: {token}")
            numbers.append(page)
    if not numbers:
        raise ValueError("pages 不能为空")
    return sorted(set(numbers))


def _extract_pdf_pages(path: Path, pages: str) -> Path:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError("按页打印 PDF 需要 pypdf：pip install pypdf") from exc

    page_numbers = _parse_page_numbers(pages)
    reader = PdfReader(str(path))
    total = len(reader.pages)
    if total == 0:
        raise ValueError("PDF 没有可打印的页面")
    for page in page_numbers:
        if page > total:
            raise ValueError(f"页码 {page} 超出 PDF 总页数 {total}")

    writer = PdfWriter()
    for page in page_numbers:
        writer.add_page(reader.pages[page - 1])

    handle, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="coara_print_")
    tmp_path = Path(tmp_name)
    try:
        with open(handle, "wb") as handle_io:
            writer.write(handle_io)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _resolve_print_path(path: Path, pages: str) -> tuple[Path, Path | None]:
    if not pages:
        return path, None
    tmp_path = _extract_pdf_pages(path, pages)
    return tmp_path, tmp_path


def _find_sumatra_exe() -> Path | None:
    candidates: list[Path] = []
    env_path = (os.environ.get("COARA_SUMATRA_PATH") or "").strip()
    if env_path:
        candidates.append(Path(env_path))
    which = shutil.which("SumatraPDF")
    if which:
        candidates.append(Path(which))
    candidates.extend(
        [
            Path(r"C:\Program Files\SumatraPDF\SumatraPDF.exe"),
            Path(r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "SumatraPDF" / "SumatraPDF.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _print_sumatra(exe: Path, print_path: Path, printer: str) -> None:
    result = _run_command(
        [str(exe), "-silent", "-exit-when-done", "-print-to", printer, str(print_path)],
        timeout=180.0,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"退出码 {result.returncode}")


def _print_mspaint(print_path: Path, printer: str) -> None:
    mspaint = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "mspaint.exe"
    if not mspaint.is_file():
        mspaint = Path("mspaint.exe")
    result = _run_command([str(mspaint), "/pt", str(print_path), printer], timeout=180.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"退出码 {result.returncode}")


def _print_file_windows(path: Path, printer_name: str, copies: int, pages: str) -> str:
    printers = _list_printers_windows()
    target = _resolve_target_printer(printer_name, printers)
    print_path, cleanup_path = _resolve_print_path(path, pages)

    sumatra = _find_sumatra_exe()
    is_pdf = print_path.suffix.lower() == ".pdf"
    try:
        if sumatra is not None:
            _run_copies(
                copies,
                lambda: _print_sumatra(sumatra, print_path, target),
                label="SumatraPDF",
            )
            backend = "SumatraPDF"
        elif is_pdf:
            raise RuntimeError(
                "打印 PDF 需要安装 SumatraPDF（https://www.sumatrapdfreader.org/），或设置环境变量 COARA_SUMATRA_PATH"
            )
        else:
            _run_copies(
                copies,
                lambda: _print_mspaint(print_path, target),
                label="MSPaint",
            )
            backend = "MSPaint"
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)

    return _format_summary(path, target, copies=copies, pages=pages, backend=backend)


def _list_printers_windows() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Printer | "
        "Select-Object Name, Default, PrinterStatus, WorkOffline | "
        "ConvertTo-Json -Compress"
    )
    result = _run_command(["powershell", "-NoProfile", "-Command", script], timeout=60.0)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or "无法枚举 Windows 打印机")

    payload = (result.stdout or "").strip()
    if not payload:
        return []

    data = json.loads(payload)
    if isinstance(data, dict):
        data = [data]

    printers: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        code = item.get("PrinterStatus")
        offline = bool(item.get("WorkOffline"))
        if offline:
            status = "offline"
        elif code is None:
            status = "unknown"
        else:
            status = _WIN_STATUS.get(int(code), f"status_{code}")
        printers.append(
            {
                "name": name,
                "is_default": bool(item.get("Default")),
                "status": status,
            }
        )
    printers.sort(key=lambda row: (not row["is_default"], row["name"].lower()))
    return printers


def _list_printers_cups() -> list[dict[str, Any]]:
    if shutil.which("lpstat") is None:
        raise RuntimeError("未找到 lpstat，请安装 CUPS 客户端")

    default_name = ""
    default_result = _run_command(["lpstat", "-d"], timeout=30.0)
    if default_result.returncode == 0:
        line = (default_result.stdout or "").strip()
        if ":" in line:
            default_name = line.split(":", 1)[1].strip()

    result = _run_command(["lpstat", "-p"], timeout=30.0)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or "无法枚举 CUPS 打印机")

    printers: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("printer "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if "disabled" in line:
            status = "disabled"
        elif "printing" in line:
            status = "printing"
        elif "idle" in line:
            status = "idle"
        else:
            status = "unknown"
        printers.append({"name": name, "is_default": name == default_name, "status": status})

    if not printers and default_name:
        printers.append({"name": default_name, "is_default": True, "status": "unknown"})

    printers.sort(key=lambda row: (not row["is_default"], row["name"].lower()))
    return printers


def _print_file_cups(path: Path, printer_name: str, copies: int, pages: str) -> str:
    if shutil.which("lp") is None:
        raise RuntimeError("未找到 lp 命令，请安装 CUPS 客户端")

    printers = _list_printers_cups()
    target = _resolve_target_printer(printer_name, printers)

    args = ["lp", "-d", target, "-n", str(copies)]
    if pages:
        args.extend(["-o", f"page-ranges={pages}"])
    args.append(str(path))

    result = _run_command(args, timeout=120.0)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "CUPS 打印失败")

    return _format_summary(
        path,
        target,
        copies=copies,
        pages=pages,
        extra=(result.stdout or "").strip(),
    )
