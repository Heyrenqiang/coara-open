"""RemoteEventBus: 远端事件源——界面骨架的 EventBus 替身"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from src.cli.attach_transport import AttachTransport
from src.core.events import TraceEvent
from src.core.logger import logger
from src.core.process import schedule_threadsafe

SyncSubscriber = Callable[[TraceEvent], None]


def _event_from_ws_frame(frame: dict[str, Any]) -> TraceEvent:
    """把服务端 WS 事件帧转换为 TraceEvent"""
    if frame.get("event_type"):
        # 旧协议：字段齐全的 TraceEvent 字典。
        return TraceEvent.from_dict(frame)
    event_type = str(frame.get("type") or "")
    if not event_type:
        raise ValueError("事件帧缺 type")
    coara_id = str(frame.get("coara_id") or "")
    payload = {key: value for key, value in frame.items() if key not in ("type", "coara_id") and value is not None}
    return TraceEvent(
        coara_id=coara_id,
        coara_name="",
        event_type=event_type,
        message=str(frame.get("message") or ""),
        payload=payload,
    )


# 服务端事件帧保真的 topic（type 命名对齐点，详见 attach_transport 协议注释）。
KNOWN_TOPICS: tuple[str, ...] = (
    "workspace_switched",
    "session_started",
    "output_truncation_recovery",
    "subagent_start",
    "subagent_complete",
    "subagent_failed",
    "flow_started",
    "flow_finished",
    "background_agent_start",
    "background_agent_complete",
    "background_task_complete",
    "tool_start",
    "tool_complete",
    "process_spawned",
    "coara_created",
    "coara_terminated",
    "thinking_progress",
    "llm_turn_complete",
    "llm_request_start",
    "llm_switched",
    "plan_mode_changed",
    "event_turn_start",
    "event_turn_complete",
    "continuation_input_received",
    "continuation_input_injected",
    "session_auto_new",
    "completed",
    "turn_failed",
    "turn_interrupted",
    "user_message",
    "chat_chunk",
    "conversation_message",
)


class Subscription:
    """Unsubscribe token returned by RemoteEventBus.subscribe()."""

    def __init__(self, bus: RemoteEventBus, subscriber_id: str, topic: str | None) -> None:
        self._bus = bus
        self._subscriber_id = subscriber_id
        self._topic = topic

    def unsubscribe(self) -> None:
        self._bus._remove_subscription(self._subscriber_id, self._topic)


class RemoteEventBus:
    """Subscribe-only event bus fed by remote kernel event frames."""

    def __init__(self, transport: AttachTransport, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._transport = transport
        try:
            self._loop: asyncio.AbstractEventLoop | None = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = loop
        self._subscribers: dict[str | None, list[tuple[str, SyncSubscriber]]] = defaultdict(list)
        self._counter = 0
        self._connected = bool(getattr(transport, "connected", False))
        self._connection_listeners: list[Callable[[bool], None]] = []
        transport.register_handler(self._on_frame)

    @property
    def connected(self) -> bool:
        """当前到内核的连接状态（断连时界面可降级）。"""
        return self._connected

    def add_connection_listener(self, listener: Callable[[bool], None]) -> None:
        """注册连接状态变化回调（True=已连接，False=断连）。"""
        if listener not in self._connection_listeners:
            self._connection_listeners.append(listener)

    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        for listener in list(self._connection_listeners):
            try:
                listener(connected)
            except Exception as exc:
                logger.warning(f"RemoteEventBus: connection listener failed: {exc}")

    def subscribe(self, callback: SyncSubscriber, topic: str | None = None) -> Subscription:
        """Subscribe to events. topic=None means all events."""
        self._counter += 1
        subscriber_id = f"remote_sub_{self._counter}"
        self._subscribers[topic].append((subscriber_id, callback))
        return Subscription(bus=self, subscriber_id=subscriber_id, topic=topic)

    def _remove_subscription(self, subscriber_id: str, topic: str | None) -> None:
        if topic in self._subscribers:
            self._subscribers[topic] = [(sid, cb) for sid, cb in self._subscribers[topic] if sid != subscriber_id]

    def _on_frame(self, frame: dict[str, Any]) -> None:
        """Transport 入站帧回调（可能在非事件循环线程）——转入事件循环分发。"""
        frame_type = str(frame.get("type") or "")
        if frame_type == "connection_state":
            self._schedule(lambda: self._set_connected(bool(frame.get("connected"))))
            return
        if frame_type == "trace_batch":
            # 服务端微批协议：events 数组内每元素是一条扁平事件帧。
            events = frame.get("events")
            if isinstance(events, list):
                for item in events:
                    if isinstance(item, dict):
                        self._ingest_event_frame(item)
            return
        if frame_type == "event":
            # 旧单条协议兜底（测试/对接期注入仍走这里）；缺 event_type 视为畸形丢弃。
            if frame.get("event_type"):
                self._ingest_event_frame(frame)
            return
        if not frame_type:
            # 裸扁平事件帧（无 type 包裹）：宽松放行。
            self._ingest_event_frame(frame)

    def _ingest_event_frame(self, frame: dict[str, Any]) -> None:
        try:
            event = _event_from_ws_frame(frame)
        except Exception as exc:
            logger.warning(f"RemoteEventBus: malformed event frame dropped: {exc}")
            return
        self._schedule(lambda: self._dispatch(event))

    def _schedule(self, fn: Callable[[], None]) -> None:
        schedule_threadsafe(self._loop, fn)

    def _dispatch(self, event: TraceEvent) -> None:
        """在绑定的事件循环上下文内分发事件给匹配订阅者。"""
        topic = event.event_type
        subscribers = [*self._subscribers.get(topic, []), *self._subscribers.get(None, [])]
        for sub_id, callback in subscribers:
            try:
                callback(event)
            except Exception as exc:
                logger.warning(f"RemoteEventBus: subscriber '{sub_id}' failed for event '{topic}': {exc}")

    def feed_event(self, event: TraceEvent) -> None:
        """直接注入一个事件（不经 transport）——测试与本地回放用。"""
        self._schedule(lambda: self._dispatch(event))
