"""RemoteEventBus 单元测试：mock transport 验证事件分发 / topic 过滤 /
连接状态通知 / 线程安全转入事件循环。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.cli.remote_event_bus import KNOWN_TOPICS, RemoteEventBus
from src.core.events import TraceEvent


class MockTransport:
    """AttachTransport 假实现：记录发送帧，支持测试侧注入入站帧。"""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    async def send(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)

    def register_handler(self, handler: Any) -> None:
        if handler not in self.handlers:
            self.handlers.append(handler)

    async def close(self) -> None:
        self.closed = True

    # --- 测试辅助 ---

    def inject(self, frame: dict[str, Any]) -> None:
        for handler in list(self.handlers):
            handler(frame)

    def inject_event(
        self,
        event_type: str,
        *,
        coara_id: str = "c-1",
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.inject(
            {
                "type": "event",
                "event_type": event_type,
                "coara_id": coara_id,
                "coara_name": "test",
                "message": message,
                "payload": payload or {},
            }
        )


def _event(event_type: str, **kwargs: Any) -> TraceEvent:
    return TraceEvent(coara_id="c-1", coara_name="test", event_type=event_type, message="", **kwargs)


async def _flush() -> None:
    # call_soon_threadsafe 排队的回调两轮 sleep 内必执行。
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestSubscribeAndDispatch:
    def test_topic_filtered_dispatch(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic="tool_start")

        transport.inject_event("tool_start", payload={"tool_name": "shell"})
        transport.inject_event("tool_complete")

        assert len(received) == 1
        assert received[0].event_type == "tool_start"
        assert received[0].payload["tool_name"] == "shell"

    def test_wildcard_subscription_receives_all(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)

        transport.inject_event("tool_start")
        transport.inject_event("completed")
        transport.inject_event("chat_chunk")

        assert [e.event_type for e in received] == ["tool_start", "completed", "chat_chunk"]

    def test_trace_batch_dispatch(self) -> None:
        """服务端实际协议：trace_batch 微批帧，events 数组逐条分发。"""
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)

        transport.inject(
            {
                "type": "trace_batch",
                "events": [
                    {"type": "tool_start", "coara_id": "c-1", "tool": "shell", "call_id": "k1", "args": {}},
                    {"type": "completed", "coara_id": "c-1"},
                    {"type": "turn_end", "coara_id": "c-1", "turn_id": "t-1", "reason": "complete"},
                ],
            }
        )

        assert [e.event_type for e in received] == ["tool_start", "completed", "turn_end"]
        assert received[0].coara_id == "c-1"
        # 扁平帧的非保留字段全部进 payload。
        assert received[0].payload["tool"] == "shell"
        assert received[0].payload["call_id"] == "k1"
        assert received[2].payload == {"turn_id": "t-1", "reason": "complete"}

    def test_trace_batch_malformed_items_skipped(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)

        transport.inject(
            {
                "type": "trace_batch",
                "events": ["not-a-dict", {"coara_id": "c-1"}, {"type": "completed", "coara_id": "c-1"}],
            }
        )

        assert [e.event_type for e in received] == ["completed"]

    def test_unsubscribe_stops_delivery(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        sub = bus.subscribe(received.append, topic="tool_start")

        transport.inject_event("tool_start")
        sub.unsubscribe()
        transport.inject_event("tool_start")

        assert len(received) == 1

    def test_event_object_fields_preserved(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)

        transport.inject(
            {
                "type": "event",
                "event_type": "llm_switched",
                "coara_id": "root-1",
                "coara_name": "Coara",
                "message": "switched",
                "level": "info",
                "timestamp": "2026-08-30T10:00:00",
                "payload": {"provider": "deepseek", "model": "deepseek-chat"},
            }
        )

        assert len(received) == 1
        event = received[0]
        assert event.coara_id == "root-1"
        assert event.coara_name == "Coara"
        assert event.message == "switched"
        assert event.timestamp == "2026-08-30T10:00:00"
        assert event.payload == {"provider": "deepseek", "model": "deepseek-chat"}

    def test_malformed_frame_dropped(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)

        transport.inject({"type": "event", "event_type": "tool_start"})  # 缺 coara_id/message
        transport.inject({"type": "event"})  # 缺 event_type
        transport.inject({"type": "not_an_event"})

        assert received == []

    def test_all_known_topics_dispatch(self) -> None:
        """KNOWN_TOPICS 里声明的保真 topic 全部能正常分发。"""
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[str] = []
        bus.subscribe(lambda e: received.append(e.event_type), topic=None)

        for topic in KNOWN_TOPICS:
            transport.inject_event(topic)

        assert received == list(KNOWN_TOPICS)

    def test_subscriber_exception_does_not_break_others(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []

        def bad(_: TraceEvent) -> None:
            raise RuntimeError("boom")

        bus.subscribe(bad, topic="tool_start")
        bus.subscribe(received.append, topic="tool_start")
        transport.inject_event("tool_start")

        assert len(received) == 1


class TestEventLoopDispatch:
    @pytest.mark.asyncio
    async def test_frames_dispatch_inside_event_loop(self) -> None:
        """事件循环内创建的 bus：帧回调经 call_soon_threadsafe 转入循环分发。"""
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)

        transport.inject_event("tool_start")
        assert received == []  # 尚未轮到回调执行
        await _flush()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_feed_event_dispatches(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        received: list[TraceEvent] = []
        bus.subscribe(received.append, topic=None)
        bus.feed_event(_event("completed"))
        await _flush()
        assert len(received) == 1


class TestConnectionState:
    def test_initial_state_from_transport(self) -> None:
        transport = MockTransport()
        assert RemoteEventBus(transport).connected is True
        transport._connected = False
        assert RemoteEventBus(transport).connected is False

    def test_connection_state_frame_notifies_listeners(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        states: list[bool] = []
        bus.add_connection_listener(states.append)

        transport.inject({"type": "connection_state", "connected": False, "reason": "disconnected"})
        assert bus.connected is False
        assert states == [False]

        transport.inject({"type": "connection_state", "connected": True, "reason": "reconnected"})
        assert bus.connected is True
        assert states == [False, True]

    def test_duplicate_state_not_renotified(self) -> None:
        transport = MockTransport()
        bus = RemoteEventBus(transport)
        states: list[bool] = []
        bus.add_connection_listener(states.append)
        transport.inject({"type": "connection_state", "connected": False})
        transport.inject({"type": "connection_state", "connected": False})
        assert states == [False]
