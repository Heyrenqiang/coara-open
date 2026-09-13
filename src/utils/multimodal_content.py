"""Build multimodal message content blocks (text + images) for LLM APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.image_processor import (
    normalize_image_detail,
    process_image_bytes_for_api,
    process_image_for_api,
)


def image_block_from_base64(data: str, media_type: str) -> dict[str, Any]:
    """Anthropic-style image block (also converted to OpenAI in openai provider)."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def image_block_from_path(path: str | Path, detail: str | None = None) -> dict[str, Any]:
    """Read and compress an image file into a content block."""
    b64, media_type, _meta = process_image_for_api(path, detail=detail)
    return image_block_from_base64(b64, media_type)


def image_block_from_bytes(data: bytes, detail: str | None = None) -> dict[str, Any]:
    """Process raw image bytes into a content block."""
    b64, media_type, _meta = process_image_bytes_for_api(data, detail=detail)
    return image_block_from_base64(b64, media_type)


def image_bytes_blocks_with_notice(
    data: bytes, detail: str | None = None
) -> list[dict[str, Any]]:
    """同上，但输入是原始字节。"""
    b64, media_type, meta = process_image_bytes_for_api(data, detail=detail)
    blocks: list[dict[str, Any]] = []
    notice = _resize_notice_meta(meta, detail)
    if notice:
        blocks.append({"type": "text", "text": notice})
    blocks.append(image_block_from_base64(b64, media_type))
    return blocks


def _resize_notice_meta(meta: dict[str, Any], detail: str | None) -> str:
    """根据加工元数据生成缩放提示文本（未缩放/无尺寸信息返回空）。"""
    if not meta.get("resized"):
        return ""
    ow = meta.get("original_width")
    oh = meta.get("original_height")
    dw = meta.get("display_width")
    dh = meta.get("display_height")
    if not (ow and oh and dw and dh):
        return ""
    d = normalize_image_detail(detail)
    return f"[图片已按 detail={d} 缩放：原 {ow}x{oh} → {dw}x{dh}]"


def build_user_content(text: str, image_blocks: list[dict[str, Any]] | None = None) -> str | list[dict[str, Any]]:
    """Merge user text and image blocks into Message.content."""
    blocks: list[dict[str, Any]] = []
    stripped = (text or "").strip()
    if stripped:
        blocks.append({"type": "text", "text": stripped})
    if image_blocks:
        blocks.extend(image_blocks)
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return stripped
    return blocks
