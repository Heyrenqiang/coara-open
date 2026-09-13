"""Shared upload / inline-file limits for Web UI handlers."""

from __future__ import annotations

from pathlib import Path

# Per-file upload cap for /api/upload
MAX_UPLOAD_FILE_BYTES = 50 * 1024 * 1024

# /api/workspace/file inline read cap (image base64 / office text / binary sniff)
MAX_INLINE_FILE_BYTES = 2 * 1024 * 1024

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})
BINARY_SUFFIXES = frozenset(
    {
        ".pdf",
        ".mp4",
        ".webm",
        ".mov",
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".exe",
        ".zip",
        ".dll",
        ".pyc",
        ".db",
    }
)

_WIN_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_upload_filename(raw: str) -> str:
    """Normalize an uploaded filename for safe on-disk use on Windows."""
    name = Path(raw.replace("\\", "/")).name.strip()
    stem = name.rstrip(" .")
    suffix = name[len(stem) :] if stem else ""
    if not stem:
        stem = "upload"
    base = stem.split(".")[0].upper()
    if base in _WIN_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}{suffix}"
