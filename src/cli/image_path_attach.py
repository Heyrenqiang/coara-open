"""Detect pasted image file paths (e.g. WeChat on Windows) and attach as vision input."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.utils.multimodal_content import image_block_from_path

_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff")
_IMAGE_EXT_PATTERN = r"(?:png|jpe?g|gif|webp|bmp|tiff)"
# Windows absolute paths to image files (drive letter required).
_IMAGE_PATH_RE = re.compile(
    rf'["\']?([A-Za-z]:[\\/][^\r\n<>|\"*?]+?\.{_IMAGE_EXT_PATTERN})["\']?',
    re.IGNORECASE,
)
# Repair WeChat-style wraps: "...102b" + newline + "1.png" -> "...102b1.png"
_BROKEN_SUFFIX_RE = re.compile(
    rf"([\w\-])(?:\r?\n)+(\d*\.{_IMAGE_EXT_PATTERN})\b",
    re.IGNORECASE,
)


def normalize_pasted_path_text(text: str) -> str:
    """Join line breaks that split an image filename extension."""
    return _BROKEN_SUFFIX_RE.sub(r"\1\2", text)


def extract_image_paths(text: str) -> list[Path]:
    """Find image file paths in pasted user text."""
    if not text or not text.strip():
        return []
    normalized = normalize_pasted_path_text(text)
    seen: set[str] = set()
    paths: list[Path] = []
    for match in _IMAGE_PATH_RE.finditer(normalized):
        raw = match.group(1).strip().strip('"').strip("'")
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(Path(raw))
    return paths


def queue_image_paths(
    paths: list[Path], pending: list[dict[str, Any]], detail: str | None = "high"
) -> int:
    """Load existing image files into pending multimodal blocks. Returns count queued."""
    queued = 0
    for path in paths:
        try:
            if not path.is_file():
                logger.warning(f"Pasted image path not found: {path}")
                continue
            pending.append(image_block_from_path(path, detail=detail))
            queued += 1
        except Exception as exc:
            logger.warning(f"Cannot load pasted image {path}: {exc}")
    return queued


def strip_image_paths_from_text(text: str, paths: list[Path]) -> str:
    """Remove detected image path strings from user-visible message text."""
    if not paths:
        return text
    cleaned = normalize_pasted_path_text(text)
    for path in paths:
        cleaned = cleaned.replace(str(path), "")
        cleaned = cleaned.replace(path.as_posix(), "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def attach_images_from_user_text(text: str, pending: list[dict[str, Any]]) -> tuple[str, int, list[Path]]:
    """Extract paths from text, queue images. Returns (cleaned_text, queued_count, paths_found)."""
    paths = extract_image_paths(text)
    if not paths:
        return text, 0, []
    queued = queue_image_paths(paths, pending)
    if queued == 0:
        return text, 0, paths
    cleaned = strip_image_paths_from_text(text, paths)
    return cleaned, queued, paths
