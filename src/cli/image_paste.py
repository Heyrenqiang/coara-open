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
# 占位符已经写进输入框。回车快照里没有它，才是用户删了；还没写进输入框的不算删。
_IN_BUFFER: list[bool] = []

# 标识：每个图片在输入栏显示 `[图片名]`（图片名用真实名，位图用「图片N」）。
# `[xx]` 方括号完整性一旦被破坏（删除/残缺）→ 视为该图片被撤回。

# Alt+V 从读剪贴板到占位符落进输入框都算在飞。回车会等到全部贴完。
_LOADING: asyncio.Event | None = None
_PASTE_LOCK: asyncio.Lock | None = None
_INFLIGHT = 0
_EPOCH = 0
_SUBMITTING = 0


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
    return _INFLIGHT > 0 or not _loading_event().is_set()


async def await_image_loading() -> None:
    await _loading_event().wait()


def composer_epoch() -> int:
    return _EPOCH


def is_submitting() -> bool:
    """回车已经拿走输入快照，正在等在飞贴图。此时不要再往输入框插占位符。"""
    return _SUBMITTING > 0


def begin_paste() -> int:
    """一次 Alt+V 开始。返回当时的代际，Ctrl+U 加代后这次贴图作废。"""
    global _INFLIGHT
    _INFLIGHT += 1
    _loading_event().clear()
    return _EPOCH


def finish_paste() -> None:
    """一次 Alt+V 结束（含占位符是否写入）。全部在飞贴图结束后回车才继续。"""
    global _INFLIGHT
    _INFLIGHT = max(0, _INFLIGHT - 1)
    if _INFLIGHT == 0:
        _loading_event().set()


def discard_unsent_images() -> None:
    """Ctrl+U：丢掉还没发出的图，并作废正在读的那次贴图。"""
    global _EPOCH
    _EPOCH += 1
    forget_image_names(_NAMES)
    _PENDING.clear()
    _NAMES.clear()
    _IN_BUFFER.clear()


def ack_latest_marker() -> None:
    """占位符已经插入输入框。"""
    if _IN_BUFFER:
        _IN_BUFFER[-1] = True


def take_pending_images() -> list[dict[str, Any]]:
    blocks = list(_PENDING)
    forget_image_names(_NAMES)
    _PENDING.clear()
    _NAMES.clear()
    _IN_BUFFER.clear()
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
    """去掉输入文本中的图片占位符 `[图片名]`"""
    if names is not None:
        for name in names:
            text = text.replace(f"[{name}]", "")
        return text.strip()
    return re.sub(r"\[[^\]]+\]", "", text).strip()


def sync_cancel_if_marker_missing(current_text: str) -> bool:
    """已写进输入框的 `[图片名]` 被删或写残 → 撤掉对应那张图"""
    present = marker_names_in_text(current_text)
    missing = [i for i, name in enumerate(_NAMES) if i < len(_IN_BUFFER) and _IN_BUFFER[i] and name not in present]
    if missing:
        forget_image_names([_NAMES[i] for i in missing])
        drop = set(missing)
        _PENDING[:] = [p for i, p in enumerate(_PENDING) if i not in drop]
        _NAMES[:] = [n for i, n in enumerate(_NAMES) if i not in drop]
        _IN_BUFFER[:] = [flag for i, flag in enumerate(_IN_BUFFER) if i not in drop]
        return True
    return False


def _remember_image(block: dict[str, Any], name: str) -> None:
    _PENDING.append(block)
    _NAMES.append(name)
    _IN_BUFFER.append(False)


async def try_queue_clipboard_image(epoch: int | None = None) -> bool:
    """Read clipboard image (bitmap or image file path) into the pending list. epoch 与 begin_paste 返回值一致"""
    from src.cli.image_path_attach import queue_image_paths
    from src.utils.clipboard_image import get_clipboard_text

    stamp = _EPOCH if epoch is None else epoch
    # 连按 Alt+V 会并发多个 paste task：读取+入队串行化，
    # 保证 _PENDING/_NAMES 追加顺序与占位符插入顺序一致。
    async with _paste_lock():
        if composer_epoch() != stamp:
            return False
        # 合并检测+读取为一次 PowerShell 调取：直接读剪贴板图，无图/失效会快速返回 None。
        payload = await get_clipboard_image_as_base64()
        if composer_epoch() != stamp:
            return False
        if payload is not None:
            b64, media_type = payload
            _remember_image(image_block_from_base64(b64, media_type), next_image_name())
            return True

        # WeChat / some apps put a file path on the clipboard instead of bitmap data.
        clip_text = await get_clipboard_text()
        if composer_epoch() != stamp:
            return False
        if clip_text:
            paths = extract_image_paths(clip_text.strip())
            loaded: list[str] = []
            for path in paths:
                if queue_image_paths([path], _PENDING) > 0:
                    loaded.append(str(path))
            if loaded:
                for hint in loaded:
                    _NAMES.append(next_image_name(hint))
                    _IN_BUFFER.append(False)
                return True
        return False


async def resolve_composer_attachments(text: str) -> tuple[str, list[dict[str, Any]], list]:
    """把这次回车绑定上的图收齐：在飞贴图、已插入的占位符、正文里的图片路径"""
    global _SUBMITTING
    _SUBMITTING += 1
    try:
        await await_image_loading()
        if isinstance(text, str):
            sync_cancel_if_marker_missing(text)
        names = pending_names()
        images = take_pending_images()
        cleaned = strip_markers(text, names) if isinstance(text, str) else str(text or "")
        paths_found: list = []
        if cleaned.strip():
            cleaned, path_blocks, paths_found = attach_images_from_message_text(cleaned)
            if path_blocks:
                images = [*images, *path_blocks]
        return cleaned.strip(), images, paths_found
    finally:
        _SUBMITTING = max(0, _SUBMITTING - 1)


def attach_images_from_message_text(text: str) -> tuple[str, list[dict[str, Any]], list]:
    """If the user pasted path text (default paste), convert to image attachments."""
    from src.cli.image_path_attach import attach_images_from_user_text

    blocks: list[dict[str, Any]] = []
    cleaned, _queued, paths_found = attach_images_from_user_text(text, blocks)
    return cleaned, blocks, paths_found
