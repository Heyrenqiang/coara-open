"""Matrix 图片批量聚合：短时间窗内同房间连续图片合并为一条多模态输入"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from src.core.logger import logger
from src.matrix_client.remote_vision import MAX_IMAGE_BATCH_COUNT

# 去抖窗口：最后一张图到达后静候这么久再投递（手机批量发送在百毫秒内连发）
_DEFAULT_BATCH_WINDOW_MS = 1500
# 窗口下限：防止误配置成 0 导致聚合失效（也便于测试用小窗口）
_MIN_BATCH_WINDOW_MS = 50
# 单批图片数上限（共享 MAX_IMAGE_BATCH_COUNT）：达到即立即投递，不再等窗口
MAX_BATCH_IMAGES = MAX_IMAGE_BATCH_COUNT

# 同步投递闭包：入参为合并后的全部图片块、合并 caption、按块同序的落盘落点
MediaBatchDeliverFn = Callable[[list[dict[str, Any]], str, list[dict[str, Any]]], None]


def _window_s() -> float:
    raw = os.environ.get("COARA_MATRIX_MEDIA_BATCH_MS", "").strip()
    try:
        ms = int(raw) if raw else _DEFAULT_BATCH_WINDOW_MS
    except ValueError:
        ms = _DEFAULT_BATCH_WINDOW_MS
    return max(ms, _MIN_BATCH_WINDOW_MS) / 1000


class _RoomBatch:
    __slots__ = ("blocks", "captions", "saved", "deliver", "timer")

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self.captions: list[str] = []
        self.saved: list[dict[str, Any]] = []
        self.deliver: MediaBatchDeliverFn | None = None
        self.timer: asyncio.Task[None] | None = None


class MatrixMediaBatchAggregator:
    """按房间聚合图片消息；进程级单例（bot / matrix_runner 各自进程各一份）。"""

    def __init__(self) -> None:
        self._batches: dict[str, _RoomBatch] = {}

    def add(
        self,
        room_id: str,
        *,
        blocks: list[dict[str, Any]],
        caption: str,
        deliver: MediaBatchDeliverFn,
        saved: list[dict[str, Any]] | None = None,
    ) -> None:
        if not blocks:
            return
        batch = self._batches.get(room_id)
        if batch is None:
            batch = _RoomBatch()
            self._batches[room_id] = batch
        batch.blocks.extend(blocks)
        if saved:
            batch.saved.extend(saved)
        stripped = caption.strip()
        if stripped:
            batch.captions.append(stripped)
        batch.deliver = deliver
        if batch.timer is not None and not batch.timer.done():
            batch.timer.cancel()
        if len(batch.blocks) >= MAX_BATCH_IMAGES:
            self._flush(room_id)
            return
        batch.timer = asyncio.create_task(self._flush_later(room_id))

    async def _flush_later(self, room_id: str) -> None:
        try:
            await asyncio.sleep(_window_s())
        except asyncio.CancelledError:
            return
        self._flush(room_id)

    def _flush(self, room_id: str) -> None:
        batch = self._batches.pop(room_id, None)
        if batch is None or not batch.blocks or batch.deliver is None:
            return
        if batch.timer is not None and not batch.timer.done():
            batch.timer.cancel()
        caption = "\n".join(batch.captions)
        try:
            batch.deliver(batch.blocks, caption, batch.saved)
        except Exception:
            logger.exception(f"[Matrix] media batch deliver failed (room={room_id})")

    def flush_now(self, room_id: str) -> None:
        """立即投递该房间的待聚合批次（文本/文件消息到达时调用，保序）。"""
        self._flush(room_id)


media_batch_aggregator = MatrixMediaBatchAggregator()
