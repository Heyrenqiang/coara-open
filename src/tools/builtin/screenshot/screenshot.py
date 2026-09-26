"""截图与图片压缩核心 — mss + Pillow（缺库时给 pip 提示）。

从 mcp-tools/screenshot.py 原样迁入（MCP 退役，能力转内置挂起工具）。
"""

from __future__ import annotations

import os
import tempfile


def capture_fullscreen() -> str:
    """截取所有显示器的完整屏幕，保存为 PNG，返回文件路径。"""
    try:
        import mss
        import mss.tools
    except ImportError as exc:
        raise RuntimeError("截图需要 mss 库：pip install mss") from exc
    with mss.mss() as sct:
        # monitors[0] 是所有显示器的并集
        monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        path = os.path.join(tempfile.gettempdir(), "coara_screenshot_full.png")
        mss.tools.to_png(shot.rgb, shot.size, output=path)
    return path


def capture_region(x: int, y: int, width: int, height: int) -> str:
    """截取指定区域的屏幕，保存为 PNG，返回文件路径。"""
    try:
        import mss
        import mss.tools
    except ImportError as exc:
        raise RuntimeError("截图需要 mss 库：pip install mss") from exc
    if width <= 0 or height <= 0:
        raise ValueError("width/height 必须为正数")
    with mss.mss() as sct:
        monitor = {"left": int(x), "top": int(y), "width": int(width), "height": int(height)}
        shot = sct.grab(monitor)
        path = os.path.join(
            tempfile.gettempdir(),
            f"coara_screenshot_{x}_{y}_{width}x{height}.png",
        )
        mss.tools.to_png(shot.rgb, shot.size, output=path)
    return path


def compress_image(image_path: str, quality: int = 70) -> str:
    """将图片压缩为 JPEG 格式，返回压缩后的文件路径。"""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("图片压缩需要 Pillow 库：pip install pillow") from exc
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"文件不存在: {image_path}")
    quality = max(1, min(100, int(quality)))
    img = Image.open(image_path)
    # 统一转为 RGB，避免透明通道导致 JPEG 保存失败
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    output_path = os.path.splitext(image_path)[0] + "_compressed.jpg"
    img.save(output_path, "JPEG", quality=quality, optimize=True)
    return output_path
