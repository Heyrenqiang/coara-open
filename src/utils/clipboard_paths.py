"""Read filesystem paths from the OS clipboard (Explorer / Finder file copy)."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from src.core.logger import logger
from src.utils.clipboard_image import CLIPBOARD_TIMEOUT, _command_exists, _run_command


def format_paths_for_input(paths: list[str]) -> str:
    """Join absolute paths for chat/CLI paste (quote when spaces or quotes)."""
    parts: list[str] = []
    for raw in paths:
        path = str(raw or "").strip()
        if not path:
            continue
        if any(ch.isspace() for ch in path) or '"' in path or "'" in path:
            parts.append('"' + path.replace('"', "") + '"')
        else:
            parts.append(path)
    return " ".join(parts)


async def get_clipboard_file_paths() -> list[str]:
    """Return absolute paths from a file-manager copy (CF_HDROP / equivalent).

    Empty when the clipboard has no file list (plain text / bitmap / empty).
    """
    system = platform.system()
    try:
        if system == "Windows":
            return await _paths_windows()
        if system == "Darwin":
            return await _paths_macos()
        if system == "Linux":
            return await _paths_linux()
    except Exception as exc:
        logger.debug(f"Clipboard file-path read failed: {exc}")
    return []


async def _paths_windows() -> list[str]:
    """Prefer in-process CF_HDROP (ms); fall back to PowerShell only if ctypes fails."""
    import asyncio

    try:
        return await asyncio.to_thread(_paths_windows_ctypes)
    except Exception as exc:
        logger.debug(f"ctypes CF_HDROP failed, falling back to PowerShell: {exc}")
        return await _paths_windows_powershell()


def _paths_windows_ctypes() -> list[str]:
    """Read CF_HDROP via Win32 — no PowerShell cold start.

    Must set HANDLE restype on 64-bit Python; bare cdecl truncates the drop
    handle and DragQueryFileW returns 0 (then a mistaken PowerShell fallback
    adds ~1s latency).
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    # Third arg may be NULL when querying length — use c_void_p, not LPWSTR.
    shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    shell32.DragQueryFileW.restype = wintypes.UINT

    cf_hdrop = 15
    if not user32.OpenClipboard(None):
        raise OSError(f"OpenClipboard failed (err={ctypes.GetLastError()})")
    try:
        handle = user32.GetClipboardData(cf_hdrop)
        if not handle:
            return []
        # 0xFFFFFFFF = query count
        count = int(shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
        paths: list[str] = []
        for i in range(count):
            nchars = int(shell32.DragQueryFileW(handle, i, None, 0))
            if nchars <= 0:
                continue
            buf = ctypes.create_unicode_buffer(nchars + 1)
            shell32.DragQueryFileW(handle, i, buf, nchars + 1)
            path = (buf.value or "").strip()
            if path:
                paths.append(path)
        return paths
    finally:
        user32.CloseClipboard()


async def _paths_windows_powershell() -> list[str]:
    # FileDropList needs WinForms; one path per line on stdout.
    cmd = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$list = [System.Windows.Forms.Clipboard]::GetFileDropList(); "
        "if ($list -ne $null) { $list | ForEach-Object { $_ } }"
    )
    out = await _run_command(
        ["powershell", "-NoProfile", "-STA", "-Command", cmd],
        timeout=CLIPBOARD_TIMEOUT,
    )
    return _split_path_lines(out)


async def _paths_macos() -> list[str]:
    # Finder copy puts file URLs on the pasteboard; best-effort via osascript.
    script = (
        'set out to ""\n'
        "try\n"
        "  set theItems to the clipboard as «class furl»\n"
        "on error\n"
        "  try\n"
        "    set theItems to the clipboard as list\n"
        "  on error\n"
        '    return ""\n'
        "  end try\n"
        "end try\n"
        "if class of theItems is list then\n"
        "  repeat with anItem in theItems\n"
        "    try\n"
        "      set out to out & (POSIX path of (anItem as alias)) & linefeed\n"
        "    end try\n"
        "  end repeat\n"
        "else\n"
        "  try\n"
        "    set out to POSIX path of (theItems as alias)\n"
        "  end try\n"
        "end if\n"
        "return out\n"
    )
    out = await _run_command(["osascript", "-e", script], timeout=CLIPBOARD_TIMEOUT)
    return _split_path_lines(out)


async def _paths_linux() -> list[str]:
    # xclip/wl-paste may expose text/uri-list when files were copied.
    raw: str | bytes | None = None
    if _command_exists("wl-paste"):
        raw = await _run_command(
            ["wl-paste", "--type", "text/uri-list", "--no-newline"],
            timeout=CLIPBOARD_TIMEOUT,
        )
    if not raw and _command_exists("xclip"):
        raw = await _run_command(
            ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-o"],
            timeout=CLIPBOARD_TIMEOUT,
        )
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    return _uris_to_paths(text)


def _split_path_lines(raw: str | bytes | None) -> list[str]:
    if raw is None:
        return []
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    paths: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        path = line.strip().strip("\x00")
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _uris_to_paths(raw: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("file:"):
            parsed = urlparse(line)
            path = unquote(parsed.path or "")
            # file:///C:/foo → /C:/foo on some stacks; normalize drive form
            if len(path) >= 3 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            line = path
        if not line or line in seen:
            continue
        seen.add(line)
        paths.append(line)
    return paths


def clipboard_paths_payload(paths: list[str]) -> dict[str, Any]:
    """REST 轻量响应体。"""
    return {
        "paths": list(paths),
        "text": format_paths_for_input(paths),
    }


# 长文本粘贴：超过此长度或行数 → 收成附件/路径，避免灌进输入框
LONG_TEXT_CHARS = 800
LONG_TEXT_LINES = 12


def is_long_paste_text(text: str) -> bool:
    """是否应按「长文本」收成缩略附件，而不是整段灌进输入框。"""
    body = str(text or "")
    if len(body) >= LONG_TEXT_CHARS:
        return True
    return len(body.splitlines()) >= LONG_TEXT_LINES


def save_pasted_long_text(text: str, *, directory: Path | None = None) -> Path:
    """把长文本落到本地文件，返回绝对路径（CLI 粘贴缩略用）。"""
    import tempfile
    from datetime import datetime

    body = str(text or "")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"粘贴文本-{stamp}.txt"
    if directory is not None:
        dest_dir = Path(directory)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / name
        # 同秒连贴：加序号
        if path.exists():
            for i in range(2, 100):
                candidate = dest_dir / f"粘贴文本-{stamp}-{i}.txt"
                if not candidate.exists():
                    path = candidate
                    break
    else:
        fd, name_tmp = tempfile.mkstemp(prefix="coara_paste_", suffix=".txt")
        import os

        os.close(fd)
        path = Path(name_tmp)
    path.write_text(body, encoding="utf-8")
    return path.resolve()
