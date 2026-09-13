"""端内连接（channel_id）精确路由 + 中断取消排队回合 的回归测试。

覆盖两类历史缺陷：
- 同空间多条 attach 并存时 EndRegistry 的 (source, session_id) 单槽互截
  ——route 带 channel_id 时必须各回各家（sender_for_channel 反查）。
- Ctrl+C 打断后仍在等锁的排队回合照常复活（正文串台）——interrupt 必须
  把排队回合一并标记取消，拿锁后直接收尾不发回合。
"""

from __future__ import annotations

from src.coara.end_registry import EndRegistry
from src.coara.turn_queue import QueuedTurn, TurnQueue


def _tagged_sender(tag: str, conn_id: str):
    def sender(frame: dict) -> str:
        return f"{tag}:{frame.get('text', '')}"

    sender._end_channel_id = conn_id  # type: ignore[attr-defined]
    return sender


class TestEndRegistryChannelRouting:
    def test_deliver_with_channel_id_hits_matching_connection(self) -> None:
        """同 (source, session) 下两条 attach 通道：按 channel_id 精确归位。"""
        reg = EndRegistry()
        a = _tagged_sender("A", "conn-a")
        b = _tagged_sender("B", "conn-b")
        # 后注册的 B 覆盖单槽——旧行为下 A 的帧会被 B 截走。
        reg.register("cli-attached", a, "sess-1")
        reg.register("cli-attached", b, "sess-1")

        frame = {"kind": "chunk", "text": "hello"}
        assert reg.deliver("cli-attached", "sess-1", frame, channel_id="conn-a").value == "A:hello"
        assert reg.deliver("cli-attached", "sess-1", frame, channel_id="conn-b").value == "B:hello"

    def test_deliver_without_channel_id_falls_back_to_session_slot(self) -> None:
        """无 channel_id（旧调用方/无连接语义）退化为会话槽位（最近注册）。"""
        reg = EndRegistry()
        a = _tagged_sender("A", "conn-a")
        b = _tagged_sender("B", "conn-b")
        reg.register("cli-attached", a, "sess-1")
        reg.register("cli-attached", b, "sess-1")

        assert reg.deliver("cli-attached", "sess-1", {"kind": "chunk", "text": "x"}).value == "B:x"

    def test_deliver_unknown_channel_id_reports_miss(self) -> None:
        """channel_id 指向已断连的 attach 连接：索引 miss 后会话槽位恰是
        其它连接的同端 sender——回退会造成串台，此时不回退，返回 None。"""
        reg = EndRegistry()
        b = _tagged_sender("B", "conn-b")
        reg.register("cli-attached", b, "sess-1")

        # conn-gone 从未注册过：段 channel_id 指向不存在的连接，
        # 但同会话槽位被 conn-b 占着—— attach 段归属精确，宁丢不串。
        outcome = reg.deliver("cli-attached", "sess-1", {"kind": "chunk", "text": "x"}, channel_id="conn-gone")
        assert outcome.hit is False

    def test_deliver_channel_id_falls_back_for_untagged_end(self) -> None:
        """未打标端（matrix 等）段带 channel_id 时回退会话槽位，不丢帧。"""
        reg = EndRegistry()
        plain = lambda frame: "plain"  # noqa: E731
        reg.register("matrix", plain, "sess-1")

        outcome = reg.deliver("matrix", "sess-1", {"kind": "chunk", "text": "x"}, channel_id="!room:local")
        assert outcome.value == "plain"

    def test_sender_for_channel_ignores_untagged_senders(self) -> None:
        """未打 _end_channel_id 标的 sender：channel 精确查不入索引，
        但该端无任何打标通道时回退会话槽位（matrix 类单通道端语义）。"""
        reg = EndRegistry()
        plain = lambda frame: "plain"  # noqa: E731
        reg.register("cli-attached", plain, "sess-1")

        # 该端无打标通道：回退会话槽位，不丢帧
        assert reg.sender_for_channel("cli-attached", "sess-1", "conn-a") is not None
        # 无 channel_id 直接会话槽位
        assert reg.sender_for_channel("cli-attached", "sess-1") is not None
        assert reg.sender_for_channel("cli-attached", "sess-1") is not None


class TestQueuedTurnCancellation:
    def test_cancel_all_marks_and_clears_pending(self) -> None:
        q = TurnQueue()
        t1 = QueuedTurn(turn_id="t1", source="cli-attached")
        t2 = QueuedTurn(turn_id="t2", source="cli-attached")
        q._pending.extend([t1, t2])

        cancelled = q.cancel_all()
        assert cancelled == [t1, t2]
        assert q.depth == 0
        # 调用方负责置 cancelled 标记（cancel_all 只记账移除）。
        for item in cancelled:
            item.cancelled = True
        assert t1.cancelled and t2.cancelled

    def test_interrupt_marks_queued_turns_cancelled(self, tmp_path) -> None:
        """interrupt_current_turn 在取消运行中回合的同时，把等锁排队回合标 cancelled。"""
        from tests.helpers import make_test_coara

        coara = make_test_coara(tmp_path)
        queued = QueuedTurn(turn_id="queued-1", source="cli-attached")
        coara._turn_queue._pending.append(queued)

        # 无活跃 runtime 时返回 False，但排队项必须已被清出队列且标 cancelled。
        result = coara.interrupt_current_turn("user_ctrl_c")
        assert result is False
        assert queued.cancelled is True
        assert coara._turn_queue.depth == 0
