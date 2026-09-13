"""内核级输出路由单点（方案 D3）。

收敛「这条输出该投到哪个端」的分散判定为一处。投递侧：TurnStream 广播的
两套路由（web 单活跃顶替 / attach 按发起连接）经 :class:`EndRoute` 统一描述，
帧格式与各端行为不变。可见性侧（哪个端显示）：CLI 镜像判定已收敛到
:func:`src.coara.turn_source.cli_shows_source` 单一事实源，web 投递经
EndRegistry（正文）与收尾镜像（后台回投），matrix 经房间直投。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EndRoute:
    """一条输出的目标端路由：source（web/cli-attached/…）+ 连接标识。

    - web 主会话：source 以 ``web`` 开头、channel_id 为空 → 路由到该 source
      的当前活跃连接（单活跃顶替模型，新标签接管旧标签）。
    - attach 外挂：source=``cli-attached``、channel_id=conn_id → 定向路由到
      发起它的那条外挂 CLI 连接（多连接并存、互不串扰）。
    """

    source: str
    channel_id: str = ""

    @property
    def is_attach(self) -> bool:
        return self.source == "cli-attached" and bool(self.channel_id)
