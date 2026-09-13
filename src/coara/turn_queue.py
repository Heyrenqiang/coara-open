"""per-workspace 显式 turn 队列（方案 D5 先来后到唯一事实源）。

同一 workspace 任意时刻只有一个 turn 在跑——串行由 ``CoaraBase._process_lock``
承载（asyncio.Lock 等待者按唤醒序，天然 FIFO）。本模块把「匿名等锁者」升级为
可枚举、可观测的显式队列：入队登记（turn_id/source/入队时刻）、出队移除、
深度与内容查询，供各端展示排队状态与先后次序。

队列对象只做记账与观测，不改变串行行为（锁仍是执行裁决点）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class QueuedTurn:
    """一个等待串行锁的回合。"""

    turn_id: str
    source: str
    enqueued_at: float = field(default_factory=time.time)
    # 中断时置位：拿到锁后据此直接收尾（不发回合），防打断后排队回合复活。
    cancelled: bool = False


class TurnQueue:
    """单 workspace 的待处理回合队列（FIFO 记账）。

    单事件循环线程安全（asyncio，无显式锁——与 ``_process_lock`` 同循环）。
    """

    def __init__(self) -> None:
        self._pending: list[QueuedTurn] = []

    def enqueue(self, turn_id: str, source: str) -> QueuedTurn:
        item = QueuedTurn(turn_id=turn_id, source=source)
        self._pending.append(item)
        return item

    def remove(self, item: QueuedTurn) -> None:
        """回合拿到锁开始执行时出队（按对象身份精确移除，防空 turn_id 歧义）。"""
        for i, existing in enumerate(self._pending):
            if existing is item:
                del self._pending[i]
                return

    def cancel_all(self) -> list[QueuedTurn]:
        """中断语义：清掉全部仍在等锁的排队回合并返回它们（供调用方发
        cancelled 收尾帧）。只移除记账——拿到锁在跑的回合不在此列。"""
        cancelled = list(self._pending)
        self._pending.clear()
        return cancelled

    @property
    def depth(self) -> int:
        return len(self._pending)

    def peek(self) -> list[QueuedTurn]:
        """当前排队中的回合快照（FIFO 次序）。"""
        return list(self._pending)

    def clear(self) -> None:
        self._pending.clear()
