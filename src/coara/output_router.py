"""内核级输出路由单点（方案 D3）"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EndRoute:
    """一条输出的目标端路由：source（web/cli-attached/）+ 连接标识"""

    source: str
    channel_id: str = ""

    @property
    def is_attach(self) -> bool:
        return self.source == "cli-attached" and bool(self.channel_id)
