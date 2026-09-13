"""CLI clipboard image paste — pending attachments for the next chat turn."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.cli.image_names import forget_image_names, marker_names_in_text, next_image_name
from src.cli.image_path_attach import extract_image_paths
from src.utils.clipboard_image import get_clipboard_image_as_base64
from src.utils.multimodal_content import image_block_from_base64

_PENDING: list[dict[str, Any]] = []  # image blocks, in paste order
_NAMES: list[str] = []  # parallel display names (图片N / real filename)

# 标识：每个图片在输入栏显示 `[图片名]`（图片名用真实名，位图用「图片N」）。
# `[xx]` 方括号完整性一旦被破坏（删除/残缺）→ 视为该图片被撤回。

# Alt+V 读剪贴板图是异步的：读取期间置位 _LOADING，回车时 await 等它完成。
_LOADING: asyncio.Event | None = None
_PASTE_LOCK: asyncio.Lock | None = None


def _loading_event() -> asyncio.Event:
    global _LOADING
    if _LOADING is None:
        _LOADING = asyncio.Event()
        _LOADING.set()  # idle
    return _LOADING


def _paste_lock() -> asyncio.Lock:
    global _PASTE_LOCK
    if _PASTE_LOCK is None:
        _PASTE_LOCK = asyncio.Lock()
    return _PASTE_LOCK


def is_image_loading() -> bool:
    return not _loading_event().is_set()


async def await_image_loading() -> None:
    await _loading_event().wait()


def take_pending_images() -> list[dict[str, Any]]:
    blocks = list(_PENDING)
    forget_image_names(_NAMES)
    _PENDING.clear()
    _NAMES.clear()
    return blocks


def pending_names() -> list[str]:
    """Display names for pending clipboard images (parallel to ``_PENDING``)."""
    return list(_NAMES)


def _marker_str(idx: int) -> str:
    return f"[{_NAMES[idx]}]"


def paste_marker() -> str:
    """当前（最新）一张图的标识 `[图片名]`；无图返空。"""
    if _PENDING:
        return _marker_str(len(_PENDING) - 1)
    return ""


def strip_markers(text: str, names: list[str] | None = None) -> str:
    """去掉输入文本中的图片占位符 `[图片名]`。

    names 给定 → 只删这些已知图片名的占位符（避免误删用户正文里的其它 `[...]`，
    如 `[重要]`）；names 为 None → 保留旧行为（删所有方括号标记，兼容其它调用）。
    """
    if names:
        for name in names:
            text = text.replace(f"[{name}]", "")
        return text.strip()
    return re.sub(r"\[[^\]]+\]", "", text).strip()


def sync_cancel_if_marker_missing(current_text: str) -> bool:
    """每个 `[图片名]` 对应一张图；方括号标识不完整（被删/破坏）→ 删对应那张图。

    只按已知图片名匹配（图片占位符），避免用户正文里的其它 `[...]` 文本误伤。
    返回是否发生了裁剪。"""
    present = marker_names_in_text(current_text)
    missing = [i for i, name in enumerate(_NAMES) if name not in present]
    if missing:
        forget_image_names([_NAMES[i] for i in missing])
        _PENDING[:] = [p for i, p in enumerate(_PENDING) if i not in missing]
        _NAMES[:] = [n for i, n in enumerate(_NAMES) if i not in missing]
        return True
    return False


async def try_queue_clipboard_image() -> bool:
    """Read clipboard image (bitmap or image file path) into the pending list."""
    from src.cli.image_path_attach import queue_image_paths
    from src.utils.clipboard_image import get_clipboard_text

    ev = _loading_event()
    ev.clear()
    try:
        # 连按 Alt+V 会并发多个 paste task：读取+入队串行化，
        # 保证 _PENDING/_NAMES 追加顺序与占位符插入顺序一致。
        async with _paste_lock():
            # 合并检测+读取为一次 PowerShell 调取：直接读剪贴板图，无图/失效会快速返回 None。
            payload = await get_clipboard_image_as_base64()
            if payload is not None:
                b64, media_type = payload
                _PENDING.append(image_block_from_base64(b64, media_type))
                _NAMES.append(next_image_name())
                return True

            # WeChat / some apps put a file path on the clipboard instead of bitmap data.
            clip_text = await get_clipboard_text()
            if clip_text:
                paths = extract_image_paths(clip_text.strip())
                if paths:
                    n = queue_image_paths(paths, _PENDING)
                    if n > 0:
                        _NAMES.extend(next_image_name(str(p)) for p in paths[:n])
                        return True
            return False
    finally:
        ev.set()


def attach_images_from_message_text(text: str) -> tuple[str, list[dict[str, Any]], list]:
    """If the user pasted path text (default paste), convert to image attachments."""
    from src.cli.image_path_attach import attach_images_from_user_text

    blocks: list[dict[str, Any]] = []
    cleaned, _queued, paths_found = attach_images_from_user_text(text, blocks)
    return cleaned, blocks, paths_found
