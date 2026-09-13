"""图片加工 detail/patch 预算与 resize notice 测试。"""

from __future__ import annotations

import io

from src.utils.image_processor import (
    normalize_image_detail,
    process_image_bytes_for_api,
    prompt_image_output_dimensions_for_limits,
    prompt_image_resize_limits,
)
from src.utils.multimodal_content import image_bytes_blocks_with_notice


def _tiny_png(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# ── detail 归一 / 预算 ──────────────────────────────────────────


def test_normalize_image_detail():
    assert normalize_image_detail(None) == "high"
    assert normalize_image_detail("auto") == "high"
    assert normalize_image_detail("HIGH") == "high"
    assert normalize_image_detail("original") == "original"
    assert normalize_image_detail("low") == "low"


def test_prompt_image_resize_limits():
    assert prompt_image_resize_limits("high") == (2048, 2500)
    assert prompt_image_resize_limits("original") == (6000, 10000)
    assert prompt_image_resize_limits("low") == (1024, 1000)
    assert prompt_image_resize_limits("auto") == (2048, 2500)


# ── patch 面积预算计算 ─────────────────────────────────────────


def test_output_dimensions_small_image_stays():
    # 小图在预算内不缩
    assert prompt_image_output_dimensions_for_limits(512, 512, 2048, 2500) == (512, 512)


def test_output_dimensions_scales_long_edge():
    # 超长边按 max_dimension 缩
    w, h = prompt_image_output_dimensions_for_limits(4096, 1024, 2048, 2500)
    assert max(w, h) <= 2048
    assert w >= h


def test_output_dimensions_patch_budget_caps():
    # 超大图按 patch 面积预算收敛
    w, h = prompt_image_output_dimensions_for_limits(8000, 8000, 6000, 1000)
    pw = (w + 31) // 32
    ph = (h + 31) // 32
    assert pw * ph <= 1000
    assert w <= 6000 and h <= 6000


# ── 带 detail 的实际加工 ───────────────────────────────────────


def test_process_image_bytes_with_detail_resizes():
    b64, mime, meta = process_image_bytes_for_api(_tiny_png(4000, 4000), detail="high")
    assert meta.get("resized") is True
    assert meta.get("detail") == "high"
    assert meta["display_width"] <= 2048 and meta["display_height"] <= 2048
    assert b64.startswith("data:image") is False  # process_image_bytes_for_api 返回纯 base64
    assert mime.startswith("image/")


def test_process_image_bytes_without_detail_unchanged():
    b64, mime, meta = process_image_bytes_for_api(_tiny_png(64, 64))
    assert meta.get("resized") is None
    assert meta["display_width"] == 64


# ── resize notice 产出 ─────────────────────────────────────────


def test_image_bytes_blocks_with_notice_when_resized():
    blocks = image_bytes_blocks_with_notice(_tiny_png(4000, 4000), detail="high")
    assert blocks[0]["type"] == "text"
    assert "已按" in blocks[0]["text"] and "detail=high" in blocks[0]["text"]
    assert blocks[1]["type"] == "image"


def test_image_bytes_blocks_with_notice_no_resize_no_notice():
    blocks = image_bytes_blocks_with_notice(_tiny_png(64, 64), detail="high")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
