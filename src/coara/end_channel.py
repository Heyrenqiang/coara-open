"""Unified end-channel abstraction (phase 1).

一个「端」的抽象：source（cli/web/matrix/cli-attached）+ 具体连接标识 +
出站投递 + 审批/交互通道 + 当前视图 workspace + 是否前台。所有端
（Web/attach/matrix/CLI/后台/未来桌面）经它接入内核，使 ``turn``
不再按 conn_id/room_id 分通道特判（方案 D5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class EndChannel:
    """内核侧对一个已连接端的描述（视图 + 输入 + 出站 + 审批）。"""

    source: str  # cli / web / matrix / cli-attached / event / background
    channel_id: str = ""  # 连接标识：web conn_id / matrix room_id / attach conn
    send_text: Any = None  # 出站正文投递目标（可 callable / 通道对象）
    interaction_channel: Any = None  # RemoteInteractionChannel 或 None（审批用）
    view_workspace: str = ""  # 该端当前显示的 workspace（阶段 3 各端独立视图用）
    foreground: bool = False  # 是否前台驱动（占用处理槽）
    # 审批回执身份：matrix=发起者 MXID；web/attach=conn_id（可与 channel_id 相同）
    actor: str = ""
