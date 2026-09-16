"""Trace 事件发射 mixin（M 组）。

宿主为 CoaraBase（src/coara/base.py），属性（_trace_emitter / _active_turn /
_active_turn_source / identity / _session_agent_kind）在宿主 __init__ 初始化
（mixin 不设 __init__，只做方法容器）。
"""

from __future__ import annotations

from typing import Any

from src.coara.trace_emitter import serialize_messages_for_trace
from src.coara.turn_timing import TurnTimingRecorder
from src.core.logger import logger
from src.core.types import Message


class TraceMixin:
    """Trace 发射：trace sink 访问 / 回合事件 / 计时与最终轨迹 / 消息序列化"""

    def _emit_final_turn_traces(
        self,
        final_content: str,
        *,
        completed_message: str = "Message processing completed",
    ) -> None:
        """Emit assistant + completed trace events so Dashboard marks the turn OK."""
        text = (final_content or "").strip()
        if text:
            active_turn = self._active_turn
            turn_id = str(getattr(active_turn, "turn_id", "") or "")
            source = str(self._active_turn_source or "")
            self._emit_trace(
                "conversation_message",
                text,
                payload={
                    "role": "assistant",
                    "content": text,
                    "turn_id": turn_id,
                    "source": source,
                },
            )
            self._emit_trace(
                "final_response",
                "Produced final response",
                # 全文刚由 conversation_message 记录 这里只留预览
                payload={"content_preview": text[:200]},
            )
        self._emit_trace("completed", completed_message)

    def _emit_turn_timing(self, timing: TurnTimingRecorder) -> None:
        if timing.finished:
            return
        timing.finished = True
        payload = timing.to_payload()
        self._emit_trace(
            "turn_timing",
            "Turn timing summary",
            payload=payload,
        )
        logger.debug(
            "Turn timing turn_id={} total={:.0f}ms llm={:.0f}ms tools={:.0f}ms overhead={:.0f}ms",
            timing.turn_id,
            payload["total_ms"],
            sum(item["llm_ms"] for item in payload["iterations"]),
            sum(item["tools_ms"] for item in payload["iterations"]),
            payload["overhead_ms"],
        )

    @property
    def _trace_sink(self) -> Any:
        return self._trace_emitter.sink

    def set_trace_sink(self, sink: Any) -> None:
        self._trace_emitter.sink = sink

    def _emit_trace(
        self, event_type: str, message: str, *, payload: dict[str, Any] | None = None, level: str = "info"
    ) -> None:
        """Thin wrapper delegating to the trace emitter."""
        if self.is_flow_subject():
            origin_scope = "flow_loop"
        elif self.identity.user_facing:
            origin_scope = "main_loop"
        else:
            origin_scope = "subagent_loop"
        # 统一补当前回合 source：工具/收尾等事件此前常无 payload，发送端按端
        # 过滤会把无 source 的 completed 丢掉 → attach CLI spinner 转不停。
        # 显式给了 source 的不覆盖（user_message 等自行指定）。
        turn_source = str(self._active_turn_source or "").strip()
        if turn_source:
            if payload is None:
                payload = {"source": turn_source}
            elif not str(payload.get("source") or "").strip():
                payload = {**payload, "source": turn_source}
        # 多 CLI attach：补发起连接 channel_id，trace 只回投该连接（各端独立）。
        try:
            from src.coara.turn_context import get_turn_channel_id

            ch = str(get_turn_channel_id() or "").strip()
        except Exception:
            ch = ""
        if ch:
            if payload is None:
                payload = {"channel_id": ch}
            elif not str(payload.get("channel_id") or "").strip():
                payload = {**payload, "channel_id": ch}
        self._trace_emitter.emit(event_type, message, payload=payload, level=level, origin_scope=origin_scope)

    def _serialize_messages_for_trace(self, messages: list[Message]) -> list[dict[str, Any]]:
        return serialize_messages_for_trace(messages)
