"""Matrix remote image → coara Vision (multimodal) helpers."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.utils.image_processor import detect_image_format_from_bytes, process_image_bytes_for_api
from src.utils.multimodal_content import image_block_from_base64

_QUOTE_MXC_RE = re.compile(
    r"\[COARA_QUOTE_MXC\]\s*(mxc://[^\s\]]+)\s*\[/COARA_QUOTE_MXC\]",
    re.IGNORECASE,
)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


def is_image_upload(mime_type: str | None, filename: str | None) -> bool:
    mime = (mime_type or "").strip().lower()
    if mime.startswith("image/"):
        return True
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def strip_quote_mxc_marker(body: str) -> tuple[str, str | None]:
    """Remove optional quoted-image MXC marker from Matrix message body."""
    match = _QUOTE_MXC_RE.search(body)
    if not match:
        return body, None
    mxc = match.group(1).strip()
    cleaned = (_QUOTE_MXC_RE.sub("", body)).strip()
    return cleaned, mxc


# Matrix media download guards: timeout + size cap (via the module's existing
# None-on-error path).
_DOWNLOAD_TIMEOUT_SECONDS = 30
_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024


async def download_mxc_bytes(client: Any, mxc_url: str) -> bytes | None:
    """Download Matrix media via nio AsyncClient."""
    from nio import DownloadResponse

    try:
        resp = await asyncio.wait_for(client.download(mxc_url), timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    except Exception:
        return None
    if isinstance(resp, DownloadResponse) and resp.body:
        body = bytes(resp.body)
        if len(body) > _DOWNLOAD_MAX_BYTES:
            return None
        return body
    return None


# 远端 Matrix 入站：控制 Vision 体积，避免 128K 窗口被单张原图 base64 撑爆。
REMOTE_VISION_MAX_DIMENSION = 1280
REMOTE_VISION_MAX_BASE64_BYTES = 350_000


def image_block_from_bytes(data: bytes, mime_type: str | None = None) -> dict[str, Any]:
    detected = detect_image_format_from_bytes(data[:32])
    if mime_type and mime_type.startswith("image/"):
        media_type = mime_type
    elif detected:
        media_type = f"image/{detected}" if detected != "jpg" else "image/jpeg"
    else:
        media_type = "image/png"
    b64, processed_mime, _meta = process_image_bytes_for_api(
        data,
        max_dimension=REMOTE_VISION_MAX_DIMENSION,
        max_base64_size=REMOTE_VISION_MAX_BASE64_BYTES,
    )
    return image_block_from_base64(b64, processed_mime or media_type)


async def build_image_blocks_from_matrix_upload(
    client: Any,
    *,
    mxc_url: str,
    mime_type: str | None,
    quoted_mxc: str | None = None,
) -> list[dict[str, Any]]:
    """Download primary (and optional quoted) MXC images into multimodal blocks."""
    blocks: list[dict[str, Any]] = []
    if quoted_mxc and quoted_mxc != mxc_url:
        quoted_bytes = await download_mxc_bytes(client, quoted_mxc)
        if quoted_bytes:
            blocks.append(image_block_from_bytes(quoted_bytes))
    primary = await download_mxc_bytes(client, mxc_url)
    if primary:
        blocks.append(image_block_from_bytes(primary, mime_type))
    return blocks
