"""Clipboard image reader — read images from the system clipboard.

Ported from Claude Code's utils/imagePaste.ts.
Supports cross-platform clipboard image reading:
- Windows: PowerShell Get-Clipboard
- macOS: osascript (NSPasteboard)
- Linux: xclip / wl-paste

Usage:
    from src.utils.clipboard_image import get_image_from_clipboard

    image_path = await get_image_from_clipboard()
    if image_path:
        # Process the image...
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import platform
import shutil
import tempfile
from pathlib import Path

from src.core.logger import logger
from src.utils.win_proc import no_window_creationflags

# ── Timeout for clipboard commands ──────────────────────────────

CLIPBOARD_TIMEOUT = 10  # seconds


async def get_image_from_clipboard() -> Path | None:
    """Read an image from the system clipboard and save to a temp file.

    Returns:
        Path to the saved image file, or None if no image in clipboard.
    """
    system = platform.system()

    try:
        if system == "Windows":
            return await _get_image_windows()
        elif system == "Darwin":
            return await _get_image_macos()
        elif system == "Linux":
            return await _get_image_linux()
    except Exception as exc:
        logger.warning(f"Failed to read clipboard image: {exc}")

    return None


async def get_clipboard_text() -> str | None:
    """Read plain text from the system clipboard."""
    system = platform.system()
    try:
        if system == "Windows":
            return await _run_command(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::GetText()",
                ],
                timeout=CLIPBOARD_TIMEOUT,
            )
        if system == "Darwin":
            return await _run_command(["pbpaste"], timeout=CLIPBOARD_TIMEOUT)
        if system == "Linux":
            if _command_exists("wl-paste"):
                return await _run_command(["wl-paste", "--no-newline"], timeout=CLIPBOARD_TIMEOUT)
            if _command_exists("xclip"):
                return await _run_command(
                    ["xclip", "-selection", "clipboard", "-o"],
                    timeout=CLIPBOARD_TIMEOUT,
                )
    except Exception as exc:
        logger.debug(f"Clipboard text read failed: {exc}")
    return None


async def get_clipboard_image_as_base64() -> tuple[str, str] | None:
    """Read clipboard image and return as base64.

    Returns:
        (base64_data, media_type) or None
    """
    path = await get_image_from_clipboard()
    if path is None:
        return None

    try:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")

        suffix = path.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/png",
        }.get(suffix, "image/png")

        try:
            from src.utils.image_processor import process_image_for_api

            b64, media_type, _ = process_image_for_api(path, detail="high")
        except ImportError:
            logger.warning("Pillow 不可用，剪贴板图片未压缩直进上下文")
        except ValueError as exc:
            logger.warning(f"剪贴板图片处理失败：{exc}")
            return None
        except Exception as exc:
            logger.debug(f"PIL processing of clipboard image failed: {exc}")

        return b64, media_type

    except OSError as exc:
        logger.warning(f"Cannot read clipboard image file: {exc}")
        return None
    finally:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# ── Windows implementation ──────────────────────────────────────


async def _get_image_windows() -> Path | None:
    """Read clipboard image on Windows using PowerShell."""
    tmp_path = _create_temp_path(".png")
    cmd = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        f"$img = [System.Windows.Forms.Clipboard]::GetImage(); "
        f'if ($img) {{ $img.Save("{tmp_path}", [System.Drawing.Imaging.ImageFormat]::Png) }}'
    )
    await _run_command(
        ["powershell", "-NoProfile", "-Command", cmd],
        timeout=CLIPBOARD_TIMEOUT,
    )
    return _validate_temp_image(tmp_path)


# ── macOS implementation ───────────────────────────────────────


async def _get_image_macos() -> Path | None:
    """Read clipboard image on macOS using osascript."""
    tmp_path = _create_temp_path(".png")

    # Try PNG format first
    script = f'''
    set theType to (clipboard info) as text
    if theType contains "class PNGf" then
        set imgData to the clipboard as "class PNGf"
        set tmpFile to open for access POSIX file "{tmp_path}" with write permission
        write imgData to tmpFile
        close access tmpFile
    end if
    '''
    await _run_command(
        ["osascript", "-e", script],
        timeout=CLIPBOARD_TIMEOUT,
    )

    result = _validate_temp_image(tmp_path)
    if result:
        return result

    # Fallback: try using pngpaste if available
    tmp_path2 = _create_temp_path(".png")
    result2 = await _run_command(
        ["pngpaste", str(tmp_path2)],
        timeout=CLIPBOARD_TIMEOUT,
    )
    if result2 is not None:
        return _validate_temp_image(tmp_path2)

    return None


# ── Linux implementation ───────────────────────────────────────


async def _get_image_linux() -> Path | None:
    """Read clipboard image on Linux using xclip or wl-paste."""
    tmp_path = _create_temp_path(".png")

    # Try Wayland first (wl-paste outputs binary image data)
    if _command_exists("wl-paste"):
        raw = await _run_command(
            ["wl-paste", "-t", "image/png"],
            timeout=CLIPBOARD_TIMEOUT,
            raw=True,
        )
        if raw:
            tmp_path.write_bytes(raw)
            validated = _validate_temp_image(tmp_path)
            if validated:
                return validated

    # Try X11 (xclip outputs binary image data)
    if _command_exists("xclip"):
        raw = await _run_command(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            timeout=CLIPBOARD_TIMEOUT,
            raw=True,
        )
        if raw:
            tmp_path2 = _create_temp_path(".png")
            tmp_path2.write_bytes(raw)
            validated = _validate_temp_image(tmp_path2)
            if validated:
                return validated

    return None


# ── Utility functions ───────────────────────────────────────────


async def _run_command(
    cmd: list[str] | str,
    timeout: int = CLIPBOARD_TIMEOUT,
    shell: bool = False,
    *,
    raw: bool = False,
) -> str | bytes | None:
    """Run a command and return its stdout, or None on failure.

    Returns decoded text by default; ``raw=True`` returns the stdout bytes
    (for commands that output binary data, e.g. image data from xclip).
    """
    try:
        if shell and isinstance(cmd, str):
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=no_window_creationflags(),
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=no_window_creationflags(),
            )

        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode == 0 and stdout:
            return stdout if raw else stdout.decode("utf-8", errors="replace").strip()
        return None

    except TimeoutError:
        logger.debug(f"Clipboard command timed out: {cmd}")
        return None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug(f"Clipboard command failed: {cmd} -> {exc}")
        return None


def _create_temp_path(suffix: str = ".png") -> Path:
    """Create a temporary file path for clipboard image."""
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="coara_clipboard_")
    os.close(fd)
    return Path(name)


def _validate_temp_image(path: Path) -> Path | None:
    """Validate that a temp file contains a valid image."""
    if not path.exists():
        return None
    if path.stat().st_size == 0:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    # Check magic bytes for PNG
    try:
        header = path.read_bytes()[:8]
        if header[:4] == b"\x89PNG" or header[:3] == b"\xff\xd8\xff" or header[:4] == b"GIF8":
            return path
    except OSError:
        pass
    # If we can't verify, still return the path (might be valid)
    if path.stat().st_size > 100:  # At least 100 bytes
        return path
    with contextlib.suppress(OSError):
        path.unlink()
    return None


def _command_exists(name: str) -> bool:
    """Check if a command exists on the system."""
    return shutil.which(name) is not None
