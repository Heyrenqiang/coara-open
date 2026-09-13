"""Matrix 图片批量聚合：短时间窗内同房间连续图片合并为一条多模态输入。

背景：手机端多选/连拍发图是「每张图一条 Matrix 消息」，内核若逐张开回合，
LLM 看到的是 N 次独立发言而非一批；且张张各带一份系统注入，语义散乱。
本聚合器在去抖窗口内（默认 1.5s，最后一张到达后计时）把同房间图片的
image_blocks 与 caption 合并，一次性投递——LLM 看到一条 user message：
文本 + 图1 + 图2…（build_user_content 组多模态）。

投递顺序保证：flush 同步执行 deliver 闭包（接续入队或调度器排期均为同步
动作），文本消息处理前先 flush_now，图片批永远排在后续文本之前。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from src.core.logger import logger

# 去抖窗口：最后一张图到达后静候这么久再投递（手机批量发送在百毫秒内连发）
_DEFAULT_BATCH_WINDOW_MS = 1500
# 窗口下限：防止误配置成 0 导致聚合失效（也便于测试用小窗口）
_MIN_BATCH_WINDOW_MS = 50
# 单批图片数上限：达到即立即投递，不再等窗口
MAX_BATCH_IMAGES = 9

# 同步投递闭包：入参为合并后的全部图片块与合并 caption
MediaBatchDeliverFn = Callable[[list[dict[str, Any]], str], None]


def _window_s() -> float:
    raw = os.environ.get("COARA_MATRIX_MEDIA_BATCH_MS", "").strip()
    try:
        ms = int(raw) if raw else _DEFAULT_BATCH_WINDOW_MS
    except ValueError:
        ms = _DEFAULT_BATCH_WINDOW_MS
    return max(ms, _MIN_BATCH_WINDOW_MS) / 1000


class _RoomBatch:
    __slots__ = ("blocks", "captions", "deliver", "timer")

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self.captions: list[str] = []
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
    ) -> None:
        if not blocks:
            return
        batch = self._batches.get(room_id)
        if batch is None:
            batch = _RoomBatch()
            self._batches[room_id] = batch
        batch.blocks.extend(blocks)
        stripped = caption.strip()
        if stripped:
            batch.captions.append(stripped)
        # 同一房间连续到达的图片共享同一调度上下文，以最新一次的投递闭包为准
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
            batch.deliver(batch.blocks, caption)
        except Exception:
            logger.exception(f"[Matrix] media batch deliver failed (room={room_id})")

    def flush_now(self, room_id: str) -> None:
        """立即投递该房间的待聚合批次（文本/文件消息到达时调用，保序）。"""
        self._flush(room_id)


media_batch_aggregator = MatrixMediaBatchAggregator()
