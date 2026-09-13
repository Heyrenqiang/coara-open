"""注入分段模型（Segment Stream）——输出路由/渲染/录像带的统一数据源。

用户语义（账本比喻）：每发生一次「输入注入上下文」（首次输入 或 turn loop
迭代头 drain 到 continuation），就开启一个新 **注入段**。该段之后产生的所有
输出（正文 chunk / 工具行 / diff / 子智能体活动）归属该段，直到下一个注入点。

段是「最近一次注入端」的载体：输出跟随最新注入端，分界点插在回合内部的
两次 LLM 调用（turn）之间。CLI 永不显示其它端的段（各端独立零镜像）。

性能约束：段是纯内存的轻量标记（几个标量字段），开启/查询 O(1)，在线路径
不引入序列化、锁或额外拷贝。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Segment:
    """一次注入开启的输出段。

    ``source`` 注入端（cli/web/matrix/cli-attached/event/background）。
    ``turn_id`` 所属用户回合；``seq`` 会话内单调递增的段号（分界/回放用）。
    ``provider``/``model`` 该段注入时端选定的模型（与注入端同源，随段走）。
    """

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
    """会话级的当前段追踪器：开新段 + 读当前段（O(1)）。

    挂在 CoaraBase 上（每会话一个）。首段在 process_message 入口开启；
    后续段在 turn loop 迭代头 drain continuation 注入时开启。
    """

    current: Segment | None = None
    _seq: int = 0

    def open(
        self, source: str, *, turn_id: str = "", mid_turn: bool = False, channel_id: str = ""
    ) -> Segment:
        """开启新段并设为当前段。返回新段。"""
        self._seq += 1
        self.current = Segment(
            seq=self._seq, source=str(source or ""), turn_id=turn_id, mid_turn=mid_turn,
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
