"""回合相位跟踪：回答「回合现在卡在哪、卡了多久」.

CoaraBase 持有一个 TurnPhase（``self._turn_phase``），在 LLM 调用前后、流式
分片、工具执行等关键点更新；/status 回合中可查，watchdog 每 60s 落一条
DEBUG 快照。相位只区分「等 API / 收 API / 本地处理 / 跑工具」，等用户输入
（审批）不进相位器——那是「等人」不是「卡住」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.core.text import format_elapsed

_PHASE_LABELS = {
    "awaiting_llm": "等待模型响应",
    "receiving_llm": "接收模型响应",
    "tool_running": "执行工具",
    "processing": "本地处理中",
}


@dataclass(slots=True)
class TurnPhase:
    """单回合的当前相位（单写者：回合协程；读者：/status、watchdog）."""

    phase: str = ""
    detail: str = ""
    since: float = 0.0
    chunk_count: int = 0
    last_chunk_at: float = 0.0

    def set(self, phase: str, detail: str = "") -> None:
        """切换相位并重置计时与分片计数（同相位同 detail 重复调用无副作用）."""
        if phase == self.phase and detail == self.detail:
            return
        self.phase = phase
        self.detail = detail
        self.since = time.monotonic()
        self.chunk_count = 0
        self.last_chunk_at = 0.0

    def note_chunk(self) -> None:
        """记录一个流式分片；首片把相位从等待切到接收（detail 保留 provider/model）."""
        if self.phase != "receiving_llm":
            self.set("receiving_llm", self.detail)
        self.chunk_count += 1
        self.last_chunk_at = time.monotonic()

    def clear(self) -> None:
        self.phase = ""
        self.detail = ""
        self.since = 0.0
        self.chunk_count = 0
        self.last_chunk_at = 0.0

    def describe(self, now: float | None = None) -> str:
        """用户可见的一行描述；无相位时返回空串."""
        if not self.phase:
            return ""
        t = time.monotonic() if now is None else now
        elapsed = format_elapsed(t - self.since) if self.since else "0s"
        label = _PHASE_LABELS.get(self.phase, self.phase)
        text = label
        if self.detail:
            text += f" {self.detail}"
        text += f"（{elapsed}"
        if self.phase == "receiving_llm":
            text += f"，已收 {self.chunk_count} 分片"
        text += "）"
        return text
