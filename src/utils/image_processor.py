"""Image processor — resize, compress, and encode images for LLM API consumption.

Ported from Claude Code's imageResizer.ts + imageProcessor.ts.
Uses Pillow (PIL) for image processing with graceful fallback when unavailable.

Key features:
- Auto-resize to fit within API dimension limits
- Auto-compress to fit within API size limits
- BMP → PNG conversion
- Progressive JPEG quality reduction
- Magic bytes format detection
- Base64 encoding for multimodal LLM input
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from src.core.logger import logger

# ── Limits ──────────────────────────────────────────────────────

MAX_IMAGE_DIMENSION = 2000  # Max width/height in pixels
MAX_IMAGE_BASE64_SIZE = 5 * 1024 * 1024  # 5 MB base64 (API limit)
JPEG_QUALITY_DEFAULT = 85
JPEG_QUALITY_MIN = 30

# ── Image detail / prompt 预算（对齐 codex：32px patch 面积预算）───────────

#: 每 patch 边长。OpenAI/Anthropic 对齐按 32×32 patch 计 token/上下文占用。
PROMPT_IMAGE_PATCH_SIZE = 32

#: detail 对应的尺寸/预算预算（max_dimension=最大边长, max_patches=patch 数量上限）。
_HIGH_LIMITS = (2048, 2_500)
_ORIGINAL_LIMITS = (6_000, 10_000)
_LOW_LIMITS = (1024, 1_000)

def normalize_image_detail(detail: str | None) -> str:
    """把 detail 归一成 high/original/low；None/auto → high。"""
    d = (detail or "auto").strip().lower()
    if d == "original":
        return "original"
    if d == "low":
        return "low"
    return "high"


def prompt_image_resize_limits(detail: str | None) -> tuple[int, int]:
    """按 detail 返回 (max_dimension, max_patches)。"""
    d = normalize_image_detail(detail)
    return {"high": _HIGH_LIMITS, "original": _ORIGINAL_LIMITS, "low": _LOW_LIMITS}[d]


def prompt_image_output_dimensions_for_limits(
    width: int, height: int, max_dimension: int, max_patches: int
) -> tuple[int, int]:
    """按 codex 的 patch 面积预算计算目标尺寸（保持宽高比，向下收敛到预算内）。

    逻辑：
    - 先按 max_dimension 等比缩到最长边 ≤ max_dimension；
    - 再按 32px patch 网格面积预算 max_patches 收敛 target dims。
    """
    width = max(1, int(width))
    height = max(1, int(height))

    def _fits(w: int, h: int) -> bool:
        pw = (w + PROMPT_IMAGE_PATCH_SIZE - 1) // PROMPT_IMAGE_PATCH_SIZE
        ph = (h + PROMPT_IMAGE_PATCH_SIZE - 1) // PROMPT_IMAGE_PATCH_SIZE
        return w <= max_dimension and h <= max_dimension and pw * ph <= max_patches

    if _fits(width, height):
        return width, height

    # 先按边长缩
    scale = (max_dimension / max(width, height))
    w = max(1, round(width * scale))
    h = max(1, round(height * scale))
    if _fits(w, h):
        return w, h

    # 再按 patch 面积预算缩
    patch_size = float(PROMPT_IMAGE_PATCH_SIZE)
    area_scale = (
        patch_size * patch_size * max_patches / max(1, w * h)
    ) ** 0.5
    w = max(1, int(w * area_scale))
    h = max(1, int(h * area_scale))
    if _fits(w, h):
        return w, h
    # 最后一档兜底：等比再缩到预算内
    scale2 = area_scale
    w = max(1, int(w * scale2))
    h = max(1, int(h * scale2))
    return max(1, w), max(1, h)


# ── Magic bytes for format detection ────────────────────────────

MAGIC_SIGNATURES: dict[str, bytes] = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "gif": b"GIF87a",
    "gif89a": b"GIF89a",
    "webp": b"RIFF",  # WebP starts with RIFF...WEBP
    "bmp": b"BM",
    "tiff_le": b"II\x2a\x00",  # TIFF little-endian
    "tiff_be": b"MM\x00\x2a",  # TIFF big-endian
}


def detect_image_format_from_bytes(data: bytes) -> str | None:
    """Detect image format from magic bytes.

    Args:
        data: First few bytes of the image file.

    Returns:
        Format string ("png", "jpeg", "gif", "webp", "bmp", "tiff") or None.
    """
    if data[:8] == MAGIC_SIGNATURES["png"]:
        return "png"
    if data[:3] == MAGIC_SIGNATURES["jpeg"]:
        return "jpeg"
    if data[:6] == MAGIC_SIGNATURES["gif89a"] or data[:6] == MAGIC_SIGNATURES["gif"]:
        return "gif"
    if data[:4] == MAGIC_SIGNATURES["webp"] and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == MAGIC_SIGNATURES["bmp"]:
        return "bmp"
    if data[:4] in (MAGIC_SIGNATURES["tiff_le"], MAGIC_SIGNATURES["tiff_be"]):
        return "tiff"
    return None


# ── PIL availability check ─────────────────────────────────────

_pil_available: bool | None = None


def is_pil_available() -> bool:
    """Check if Pillow is available."""
    global _pil_available
    if _pil_available is None:
        try:
            from PIL import Image  # noqa: F401

            _pil_available = True
        except ImportError:
            _pil_available = False
            logger.info("Pillow not available — image resize/compress disabled, base64-only mode")
    return _pil_available


# Pillow resampling compatibility (module-level so all functions can use it)
try:
    from PIL import Image as _PILImage

    _LANCZOS: Any = _PILImage.Resampling.LANCZOS
except (ImportError, AttributeError):
    try:
        from PIL import Image as _PILImage

        _LANCZOS = _PILImage.LANCZOS  # type: ignore[attr-defined]
    except ImportError:
        _LANCZOS = None  # type: ignore[assignment]

# ── Core processing ────────────────────────────────────────────


def process_image_for_api(
    image_path: str | Path,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    max_base64_size: int = MAX_IMAGE_BASE64_SIZE,
    detail: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Process an image file for LLM API consumption.

    Reads the image, optionally resizes and compresses it, and returns
    base64-encoded data suitable for multimodal LLM input.

    Args:
        image_path: Path to the image file.
        max_dimension: Maximum width/height in pixels (used when ``detail`` is None).
        max_base64_size: Maximum base64 string size in bytes.
        detail: 图片明细语义（auto/low/high/original），提供时按 patch 面积预算缩放，
            覆盖 max_dimension 计算目标尺寸（对齐 codex PromptImageResizeLimits）。

    Returns:
        (base64_data, media_type, metadata)
    """
    path = Path(image_path)
    metadata: dict[str, Any] = {"path": str(path)}

    # Read raw bytes
    raw = path.read_bytes()
    metadata["original_size"] = len(raw)

    # Detect format from magic bytes
    detected_format = detect_image_format_from_bytes(raw[:32])
    metadata["detected_format"] = detected_format

    # If PIL is not available, just base64-encode the raw data — 但超大图不放行：
    # 无法压缩时原样放行会把 50MB 图变 67MB 巨串塞进上下文（Pillow 缺失属异常兜底）。
    if not is_pil_available():
        if len(raw) > max_base64_size:
            raise ValueError(
                f"图片过大（{len(raw) / (1024 * 1024):.1f}MB）且 Pillow 不可用无法压缩，"
                "请安装 Pillow 后重试"
            )
        b64 = base64.b64encode(raw).decode("ascii")
        media_type = _suffix_to_mime(path.suffix.lower())
        metadata["pil_used"] = False
        metadata["final_base64_size"] = len(b64)
        return b64, media_type, metadata

    # PIL is available — do full processing
    from PIL import Image

    img: Any = Image.open(path)
    metadata["original_width"] = img.width
    metadata["original_height"] = img.height
    metadata["original_mode"] = img.mode
    metadata["original_format"] = img.format

    # BMP → PNG conversion
    if img.format == "BMP" or detected_format == "bmp":
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        metadata["converted_from_bmp"] = True

    # Resize if needed — detail 预算（patch 面积）优先；否则旧 thumbnail 兜底。
    if detail:
        max_dim, max_patches = prompt_image_resize_limits(detail)
        target_w, target_h = prompt_image_output_dimensions_for_limits(
            img.width, img.height, max_dim, max_patches
        )
        if (target_w, target_h) != (img.width, img.height):
            img = img.resize((target_w, target_h), _LANCZOS)
            metadata["resized"] = True
            metadata["detail"] = normalize_image_detail(detail)
    elif img.width > max_dimension or img.height > max_dimension:
        img.thumbnail((max_dimension, max_dimension), _LANCZOS)
        metadata["resized"] = True

    metadata["display_width"] = img.width
    metadata["display_height"] = img.height

    # Encode and compress
    b64, media_type = _encode_and_compress(img, max_base64_size)

    metadata["pil_used"] = True
    metadata["media_type"] = media_type
    metadata["final_base64_size"] = len(b64)

    return b64, media_type, metadata


def process_image_bytes_for_api(
    image_data: bytes,
    max_dimension: int = MAX_IMAGE_DIMENSION,
    max_base64_size: int = MAX_IMAGE_BASE64_SIZE,
    detail: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Process raw image bytes for LLM API consumption.

    Same as process_image_for_api but takes bytes instead of a file path.
    """
    metadata: dict[str, Any] = {"original_size": len(image_data)}

    detected_format = detect_image_format_from_bytes(image_data[:32])
    metadata["detected_format"] = detected_format

    if not is_pil_available():
        if len(image_data) > max_base64_size:
            raise ValueError(
                f"图片过大（{len(image_data) / (1024 * 1024):.1f}MB）且 Pillow 不可用无法压缩，"
                "请安装 Pillow 后重试"
            )
        b64 = base64.b64encode(image_data).decode("ascii")
        media_type = _format_to_mime(detected_format) if detected_format else "image/png"
        metadata["pil_used"] = False
        metadata["final_base64_size"] = len(b64)
        return b64, media_type, metadata

    from PIL import Image

    img = Image.open(io.BytesIO(image_data))
    metadata["original_width"] = img.width
    metadata["original_height"] = img.height

    # Resize if needed — detail 预算优先；否则旧 thumbnail 兜底。
    if detail:
        max_dim, max_patches = prompt_image_resize_limits(detail)
        target_w, target_h = prompt_image_output_dimensions_for_limits(
            img.width, img.height, max_dim, max_patches
        )
        if (target_w, target_h) != (img.width, img.height):
            img = img.resize((target_w, target_h), _LANCZOS)
            metadata["resized"] = True
            metadata["detail"] = normalize_image_detail(detail)
    elif img.width > max_dimension or img.height > max_dimension:
        img.thumbnail((max_dimension, max_dimension), _LANCZOS)
        metadata["resized"] = True

    metadata["display_width"] = img.width
    metadata["display_height"] = img.height

    b64, media_type = _encode_and_compress(img, max_base64_size)

    metadata["pil_used"] = True
    metadata["media_type"] = media_type
    metadata["final_base64_size"] = len(b64)

    return b64, media_type, metadata


# ── Internal helpers ────────────────────────────────────────────


def _encode_and_compress(
    img: Any,  # PIL.Image.Image
    max_base64_size: int,
) -> tuple[str, str]:
    """Encode image to base64, progressively compressing if too large.

    Strategy:
    1. Try PNG (for RGBA / palette images)
    2. Try JPEG at default quality
    3. Progressively reduce JPEG quality
    4. If still too large, resize dimensions by 75% and retry
    """

    # Step 1: Try PNG for images with alpha channel
    if img.mode in ("RGBA", "P", "LA", "PA"):
        b64, size = _encode_png(img)
        if size <= max_base64_size:
            return b64, "image/png"
        # PNG too large, try JPEG
        img_for_jpeg = img.convert("RGB") if img.mode != "RGB" else img
    else:
        img_for_jpeg = img.convert("RGB") if img.mode != "RGB" else img

    # Step 2: Try JPEG at default quality
    quality = JPEG_QUALITY_DEFAULT
    b64, size = _encode_jpeg(img_for_jpeg, quality)
    if size <= max_base64_size:
        return b64, "image/jpeg"

    # Step 3: Progressively reduce quality
    while quality > JPEG_QUALITY_MIN and size > max_base64_size:
        quality = max(JPEG_QUALITY_MIN, quality - 15)
        b64, size = _encode_jpeg(img_for_jpeg, quality)

    if size <= max_base64_size:
        return b64, "image/jpeg"

    # Step 4: Resize and retry (each scale is relative to original)
    for scale in (0.75, 0.5, 0.25):
        new_w = max(1, int(img_for_jpeg.width * scale))
        new_h = max(1, int(img_for_jpeg.height * scale))
        resized = img_for_jpeg.resize((new_w, new_h), _LANCZOS)
        b64, size = _encode_jpeg(resized, JPEG_QUALITY_MIN)
        if size <= max_base64_size:
            return b64, "image/jpeg"

    # Last resort: return whatever we have
    logger.warning(f"Image still exceeds size limit after aggressive compression ({size} bytes)")
    return b64, "image/jpeg"


def _encode_png(img: Any) -> tuple[str, int]:
    """Encode image as PNG base64."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, len(b64)


def _encode_jpeg(img: Any, quality: int) -> tuple[str, int]:
    """Encode image as JPEG base64 at given quality."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, len(b64)


def _suffix_to_mime(suffix: str) -> str:
    """Map file suffix to MIME type."""
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/png",  # BMP will be converted
    }
    return mime_map.get(suffix, "image/png")


def _format_to_mime(fmt: str | None) -> str:
    """Map detected format to MIME type."""
    if fmt == "png":
        return "image/png"
    if fmt in ("jpeg", "jpg"):
        return "image/jpeg"
    if fmt in ("gif", "gif89a"):
        return "image/gif"
    if fmt == "webp":
        return "image/webp"
    if fmt == "bmp":
        return "image/png"
    return "image/png"


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


def is_image_file(path: str | Path) -> bool:
    """Return True when path looks like a supported raster image file."""
    candidate = Path(path)
    if candidate.suffix.lower() in _IMAGE_SUFFIXES:
        return True
    try:
        with candidate.open("rb") as handle:
            return detect_image_format_from_bytes(handle.read(32)) is not None
    except OSError:
        return False


def create_image_metadata_text(
    original_width: int,
    original_height: int,
    display_width: int,
    display_height: int,
) -> str:
    """Create a text description of image metadata for LLM context."""
    lines = [f"Image: {display_width}x{display_height}"]

    if original_width != display_width or original_height != display_height:
        scale_w = display_width / original_width if original_width else 0
        scale_h = display_height / original_height if original_height else 0
        lines.append(f"Original: {original_width}x{original_height}")
        lines.append(f"Scale: {scale_w:.2f}x{scale_h:.2f}")

    return "\n".join(lines)
