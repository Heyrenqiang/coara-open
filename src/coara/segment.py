"""注入分段模型（Segment Stream）——输出路由/渲染/录像带的统一数据源"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Segment:
    """一次注入开启的输出段"""

    seq: int
    source: str
    turn_id: str = ""
    # 该段是否由 mid-turn continuation 注入开启（首次输入为 False）。
    mid_turn: bool = False
    provider: str = ""
    model: str = ""
    # 端内连接标识（attach conn_id）：同端多连接并存时按它精确路由输出。
    channel_id: str = ""


@dataclass(slots=True)
class SegmentTracker:
    """会话级的当前段追踪器：开新段 + 读当前段（O(1)）"""

    current: Segment | None = None
    _seq: int = 0

    def open(self, source: str, *, turn_id: str = "", mid_turn: bool = False, channel_id: str = "") -> Segment:
        """开启新段并设为当前段。返回新段。"""
        self._seq += 1
        self.current = Segment(
            seq=self._seq,
            source=str(source or ""),
            turn_id=turn_id,
            mid_turn=mid_turn,
            channel_id=str(channel_id or ""),
        )
        return self.current

    @property
    def source(self) -> str:
        """当前段归属端（无段时回退空串）。"""
        return self.current.source if self.current is not None else ""

    @property
    def channel_id(self) -> str:
        """当前段的端内连接标识（无段时回退空串）。"""
        return self.current.channel_id if self.current is not None else ""

    def clear(self) -> None:
        self.current = None
        self._seq = 0
