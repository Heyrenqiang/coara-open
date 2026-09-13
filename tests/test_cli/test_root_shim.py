"""RootShim 单元测试：mock transport 验证快照应用、事件驱动状态镜像、
动作透传帧格式、流式回合异步生成器、命令透传回执。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.cli.root_shim import RootShim
from src.core.types import ContinuationInput


class MockTransport:
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

    def inject(self, frame: dict[str, Any]) -> None:
        for handler in list(self.handlers):
            handler(frame)

    def inject_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.inject(
            {
                "type": "event",
                "event_type": event_type,
                "coara_id": "root-1",
                "coara_name": "Coara",
                "message": "",
                "payload": payload or {},
            }
        )


def _flat_attached_frame() -> dict[str, Any]:
    """服务端 _build_attach_attached_frame 的实际帧形（扁平字段，无 snapshot 嵌套键）。"""
    return {
        "type": "attached",
        "workspace": "main",
        "workspace_id": "ws-1",
        "session": "sess-1",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "session_id": "sess-1",
        "workspace_dir": "D:/ws/main",
        "coara_id": "root-1",
        "agent_name": "Coara",
        "provider_name": "deepseek",
        "model_name": "deepseek-chat",
        "is_plan_mode": False,
        "tools_count": 20,
        "skills_count": 3,
        "active_name": "main",
        "foreground_session_id": "ws-1",
        "workspaces": [
            {"id": "ws-1", "name": "main", "path": "D:/ws/main"},
            {"id": "ws-2", "name": "side", "path": "D:/ws/side"},
        ],
        "usage": {"input_tokens": 0, "cache_hit_ratio": None},
        "context_window": 131072,
        "turn_source": "",
    }


def _snapshot() -> dict[str, Any]:
    return {
        "identity": {"name": "Coara", "coara_id": "root-1", "workspace_dir": "D:/ws/main"},
        "foreground": {
            "identity": {"name": "Coara", "coara_id": "fg-1", "workspace_dir": "D:/ws/main"},
            "session_id": "sess-1",
            "workspace_dir": "D:/ws/main",
            "provider_name": "deepseek",
            "model_name": "deepseek-chat",
            "is_plan_mode": False,
            "has_active_turn": False,
            "active_turn_source": "",
            "context_window": 131072,
            "continuation_inputs": [],
            "usage_snapshot": {"has_reported_input": False, "usage": {}, "cache_hit_ratio": None},
        },
        "workspace_manager": {
            "active_name": "main",
            "active_workspace_id": "ws-1",
            "cwd_display_suffix": "sub/dir",
            "workspaces": [
                {"id": "ws-1", "name": "main", "path": "D:/ws/main"},
                {"id": "ws-2", "name": "side", "path": "D:/ws/side"},
            ],
        },
        "sessions": {
            "ws-1": {
                "workspace_dir": "D:/ws/main",
                "workspace_name": "main",
                "session_id": "sess-1",
                "has_active_turn": False,
            }
        },
        "foreground_session_id": "ws-1",
    }


def _make_shim() -> tuple[MockTransport, RootShim]:
    transport = MockTransport()
    shim = RootShim(transport)
    return transport, shim


async def _flush() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestAttachReconnectReplay:
    """断连重连：二次握手 resume + replayed 帧按 (turn_id, seq) 去重 + 无活跃流走 _replay_callback。"""

    @pytest.mark.asyncio
    async def test_reconnect_sends_resume_handshake(self) -> None:
        """断连重连后客户端发二次握手（type=attach + resume=旧 channel_id）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", **_flat_attached_frame(), "channel_id": "conn-old"})
        await _flush()
        assert shim._attach_channel_id == "conn-old"
        # 模拟断连重连（connection_state connected=True）
        transport.inject({"type": "connection_state", "connected": True, "reason": "reconnected"})
        await _flush()
        resume_frame = next(
            (f for f in transport.sent if f.get("type") == "attach" and f.get("resume") == "conn-old"),
            None,
        )
        assert resume_frame is not None

    @pytest.mark.asyncio
    async def test_replayed_frames_deduplicated_by_seq(self) -> None:
        """重放帧按 (turn_id, seq) 去重：同 seq 重复帧跳过，不双显。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await _flush()
        server_turn_id = "srv-replay-1"
        transport.inject({"type": "turn_start", "turn_id": server_turn_id, "seq": 1})
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 2, "text": "一"})
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 3, "text": "二"})
        # 模拟断连重连后服务端重放 buffer：seq 1-3 重复到达
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 2, "text": "一", "replayed": True})
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 3, "text": "二", "replayed": True})
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 4, "text": "三", "replayed": True})
        transport.inject({"type": "turn_end", "turn_id": server_turn_id, "seq": 5, "reason": "complete"})
        await task
        assert chunks == ["一", "二", "三"]

    @pytest.mark.asyncio
    async def test_replayed_frame_no_active_stream_goes_to_replay_callback(self) -> None:
        """重放帧到达时本地流已终结：走 _replay_callback 渲染，不丢尾。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        replayed_frames: list[dict[str, Any]] = []
        shim.foreground_coara._replay_callback = lambda f: replayed_frames.append(f)

        # 无活跃流，直接注入重放帧
        transport.inject({"type": "chunk", "turn_id": "srv-orphan", "seq": 1, "text": "尾巴", "replayed": True})
        transport.inject({"type": "chunk", "turn_id": "srv-orphan", "seq": 2, "text": "尾巴2", "replayed": True})
        transport.inject(
            {"type": "turn_end", "turn_id": "srv-orphan", "seq": 3, "reason": "complete", "replayed": True}
        )
        await _flush()
        assert len(replayed_frames) == 3
        assert replayed_frames[0]["text"] == "尾巴"
        assert replayed_frames[2]["type"] == "turn_end"

    @pytest.mark.asyncio
    async def test_replayed_frame_no_replay_callback_drops_silently(self) -> None:
        """无 _replay_callback 时重放帧不抛错（兜底丢弃）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()
        # 不注册 _replay_callback
        transport.inject({"type": "chunk", "turn_id": "srv-x", "seq": 1, "text": "x", "replayed": True})
        await _flush()

    @pytest.mark.asyncio
    async def test_turn_queued_frame_sets_queued_flag(self) -> None:
        """turn_queued 帧置 stream["queued"]=True，spinner 读它显「排队中」。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await _flush()
        server_turn_id = "srv-q-1"
        transport.inject({"type": "turn_start", "turn_id": server_turn_id, "seq": 1})
        transport.inject({"type": "turn_queued", "turn_id": server_turn_id, "seq": 2})
        await _flush()
        # 排队帧已置 queued；turn_start 清掉
        streams = shim.foreground_coara._turn_streams
        assert streams  # 本地流在
        transport.inject({"type": "turn_end", "turn_id": server_turn_id, "seq": 3, "reason": "complete"})
        await task

    @pytest.mark.asyncio
    async def test_disconnect_clears_queued_and_active(self) -> None:
        """断连终结所有回合流，queued 标记不再残留。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        async def consume() -> None:
            async for _ in shim.foreground_coara.process_message("hi", source="cli"):
                pass

        task = asyncio.create_task(consume())
        await _flush()
        transport.inject({"type": "connection_state", "connected": False, "reason": "disconnected"})
        await _flush()
        await task
        assert not shim.foreground_coara.has_active_turn()


class TestSubagentFramesNotForegroundBody:
    """子智能体正文帧绝不进前台回合流（否则与结果回显行重复一遍）。

    帧型判据由服务端 _attach_output_frame 保证：主会话正文 = chunk，
    子智能体过程 = subagent_chunk / subagent_result（+ agent_kind 归属字段）。
    """

    @pytest.mark.asyncio
    async def test_live_subagent_frames_not_streamed_as_body(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await _flush()
        server_turn_id = "srv-sa-1"
        transport.inject({"type": "turn_start", "turn_id": server_turn_id, "seq": 1})
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 2, "text": "主会话正文"})
        # 子智能体过程旁白 / 最终报告：独立帧型
        transport.inject(
            {
                "type": "subagent_chunk",
                "turn_id": server_turn_id,
                "seq": 3,
                "text": "start by exploring the key files in parallel",
                "agent_kind": "subagent",
                "tool_call_id": "call-1",
            }
        )
        transport.inject(
            {
                "type": "subagent_result",
                "turn_id": server_turn_id,
                "seq": 4,
                "text": "一、竞态清单",
                "agent_kind": "subagent",
                "tool_call_id": "call-1",
            }
        )
        # 旧内核形态：并进 chunk 但带归属字段（兜底判据）
        transport.inject(
            {"type": "chunk", "turn_id": server_turn_id, "seq": 5, "text": "子智能体正文", "agent_kind": "subagent"}
        )
        transport.inject({"type": "turn_end", "turn_id": server_turn_id, "seq": 6, "reason": "complete"})
        await task
        assert chunks == ["主会话正文"]

    @pytest.mark.asyncio
    async def test_replayed_subagent_frames_not_rendered(self) -> None:
        """重放路径同样挡住：断连重连后服务端重发 buffer，子智能体帧不重播。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        replayed: list[dict[str, Any]] = []
        shim.foreground_coara._replay_callback = lambda f: replayed.append(f)

        transport.inject(
            {
                "type": "subagent_chunk",
                "turn_id": "srv-sa-x",
                "seq": 1,
                "text": "子智能体旁白",
                "agent_kind": "subagent",
                "replayed": True,
            }
        )
        transport.inject(
            {
                "type": "subagent_result",
                "turn_id": "srv-sa-x",
                "seq": 2,
                "text": "子智能体报告",
                "agent_kind": "subagent",
                "replayed": True,
            }
        )
        transport.inject(
            {
                "type": "chunk",
                "turn_id": "srv-sa-x",
                "seq": 3,
                "text": "子智能体正文",
                "agent_kind": "subagent",
                "replayed": True,
            }
        )
        transport.inject({"type": "chunk", "turn_id": "srv-sa-x", "seq": 4, "text": "主会话尾", "replayed": True})
        transport.inject({"type": "turn_end", "turn_id": "srv-sa-x", "seq": 5, "reason": "complete", "replayed": True})
        await _flush()
        assert [f["text"] for f in replayed if "text" in f] == ["主会话尾"]

    @pytest.mark.asyncio
    async def test_subagent_frames_delivered_to_fold_callback_once(self) -> None:
        """挡下的子智能体帧转交折叠块消费方；重连重发的同 seq 帧不灌两份。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        folded: list[dict[str, Any]] = []
        shim._subagent_frame_callback = lambda frame: folded.append(frame)

        for replayed in (False, True):
            transport.inject(
                {
                    "type": "subagent_chunk",
                    "turn_id": "srv-sa-1",
                    "seq": 3,
                    "text": "子智能体旁白",
                    "agent_kind": "subagent",
                    "tool_call_id": "call-1",
                    "replayed": replayed,
                }
            )
            transport.inject(
                {
                    "type": "subagent_result",
                    "turn_id": "srv-sa-1",
                    "seq": 4,
                    "text": "子智能体报告",
                    "agent_kind": "subagent",
                    "tool_call_id": "call-1",
                    "replayed": replayed,
                }
            )
        await _flush()
        assert [f["type"] for f in folded] == ["subagent_chunk", "subagent_result"]


class TestCommandRequestId:
    """P2-1：command_result 按 request_id 精确派发，超时迟到回执不串下一个等待者。"""

    @pytest.mark.asyncio
    async def test_command_request_id_exact_match(self) -> None:
        """服务端回显 request_id 时按 id 精确派发给对应等待者。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        async def reply() -> None:
            await asyncio.sleep(0)
            # 回执带 request_id（服务端回显）
            sent = [f for f in transport.sent if f.get("type") == "command"]
            if sent:
                transport.inject(
                    {
                        "type": "command_result",
                        "request_id": sent[-1]["request_id"],
                        "result": {"output": "精确匹配", "action": "none", "data": {}, "exit_session": False},
                    }
                )

        task = asyncio.create_task(shim.execute_command("/model kimi"))
        await reply()
        result = await task
        assert result.output == "精确匹配"

    @pytest.mark.asyncio
    async def test_command_late_reply_dropped_not_misassigned(self) -> None:
        """超时后迟到的回执：不再派给下一个等待者（防串台）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()

        # 第一个命令：故意超时（30s 太长，测试里手动模拟迟到回执）
        async def first_command() -> None:
            task = asyncio.create_task(shim.execute_command("/slow"))
            await asyncio.sleep(0)
            # 超时后未来再回执
            await task

        task1 = asyncio.create_task(first_command())
        await asyncio.sleep(0)
        # 不等超时，直接发第二个命令（不同 request_id）
        task2 = asyncio.create_task(shim.execute_command("/fast"))

        async def reply_fast() -> None:
            await asyncio.sleep(0)
            sent = [f for f in transport.sent if f.get("type") == "command"]
            fast_req = next(f for f in sent if f.get("text") == "/fast")
            transport.inject(
                {
                    "type": "command_result",
                    "request_id": fast_req["request_id"],
                    "result": {"output": "fast-ok", "action": "none", "data": {}, "exit_session": False},
                }
            )

        await reply_fast()
        result2 = await task2
        assert result2.output == "fast-ok"
        # 迟到回执（slow 的）不应影响 fast 的结果
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1


class TestSnapshot:
    def test_apply_attached_snapshot(self) -> None:
        transport, shim = _make_shim()
        assert shim.ready is False

        transport.inject({"type": "attached", "snapshot": _snapshot()})

        assert shim.ready is True
        assert shim.identity.name == "Coara"
        assert shim.identity.coara_id == "root-1"
        assert shim.identity.workspace_dir == "D:/ws/main"

        fg = shim.foreground_coara
        assert fg.session_id == "sess-1"
        assert fg.workspace_dir == "D:/ws/main"
        assert fg.identity.coara_id == "fg-1"
        assert fg.provider_name == "deepseek"
        assert fg.model_name == "deepseek-chat"
        assert fg.is_plan_mode is False
        assert fg.has_active_turn() is False
        assert shim._foreground_session_id == "ws-1"
        assert fg.provider.get_context_window("deepseek-chat") == 131072

        wm = shim.workspace_manager
        assert wm.active_name == "main"
        assert wm.cwd_display_suffix() == "sub/dir"
        assert wm.name_for_path("D:/ws/main") == "main"
        assert wm.name_for_path("D:/ws/side/nested") == "side"
        assert wm.name_for_path("C:/elsewhere") is None
        assert len(wm.list_workspaces()) == 2

        session = shim._sessions["ws-1"]
        assert session.session_id == "sess-1"
        assert session.workspace_dir == "D:/ws/main"
        assert session.coara.has_active_turn() is False

        assert shim.has_active_turn() is False
        assert shim.foreground_active_name() == "main"

    def test_apply_flat_attached_frame(self) -> None:
        """服务端实际帧形：扁平 attached 帧（无 snapshot 键）经归一正确填充。"""
        transport, shim = _make_shim()
        transport.inject(_flat_attached_frame())

        assert shim.ready is True
        assert shim.identity.name == "Coara"
        assert shim.identity.coara_id == "root-1"
        assert shim.identity.workspace_dir == "D:/ws/main"

        fg = shim.foreground_coara
        assert fg.session_id == "sess-1"
        assert fg.workspace_dir == "D:/ws/main"
        assert fg.identity.coara_id == "root-1"
        assert fg.provider_name == "deepseek"
        assert fg.model_name == "deepseek-chat"
        assert fg.is_plan_mode is False
        assert fg.provider.get_context_window("deepseek-chat") == 131072

        wm = shim.workspace_manager
        assert wm.active_name == "main"
        assert wm.name_for_path("D:/ws/main") == "main"
        assert wm.name_for_path("D:/ws/side/nested") == "side"
        assert len(wm.list_workspaces()) == 2

        session = shim._sessions["ws-1"]
        assert session.session_id == "sess-1"
        assert session.workspace_dir == "D:/ws/main"
        assert session.workspace_name == "main"

        assert shim._foreground_session_id == "ws-1"
        assert shim.foreground_active_name() == "main"

    def test_snapshot_frame_type_also_applies(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "snapshot", "snapshot": _snapshot()})
        assert shim.ready is True


class TestEventDrivenMirror:
    def test_llm_switched_updates_provider_model(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event(
            "llm_switched",
            {
                "provider": "kimi",
                "model": "k2",
                "context_window": 262144,
                "provider_has_key": True,
                "workspace_id": "ws-1",
                "session_id": "sess-1",
            },
        )
        fg = shim.foreground_coara
        assert fg.provider_name == "kimi"
        assert fg.model_name == "k2"
        assert fg.provider.get_context_window("k2") == 262144
        assert fg._provider_has_key is True

    def test_llm_switched_keeps_has_key_when_payload_omits_it(self) -> None:
        """旧事件无 provider_has_key 时不得清空镜像（否则 attach 误显「添加 provider」）。"""
        transport, shim = _make_shim()
        snap = _snapshot()
        snap["foreground"]["provider_has_key"] = True
        transport.inject({"type": "attached", "snapshot": snap})
        assert shim.foreground_coara._provider_has_key is True
        transport.inject_event("llm_switched", {"provider": "kimi", "model": "k2"})
        assert shim.foreground_coara.provider_name == "kimi"
        assert shim.foreground_coara._provider_has_key is True

    def test_session_started_updates_session_id(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event("session_started", {"session_id": "sess-2", "is_plan_mode": True})
        assert shim.foreground_coara.session_id == "sess-2"
        assert shim.foreground_coara.is_plan_mode is True

    def test_plan_mode_changed_updates_mirror(self) -> None:
        """plan_mode(enter/exit) 的 plan_mode_changed 事件驱动状态栏镜像开关。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        assert shim.foreground_coara.is_plan_mode is False
        transport.inject_event("plan_mode_changed", {"session_id": "sess-1", "is_plan_mode": True})
        assert shim.foreground_coara.is_plan_mode is True
        transport.inject_event("plan_mode_changed", {"session_id": "sess-1", "is_plan_mode": False})
        assert shim.foreground_coara.is_plan_mode is False

    def test_plan_mode_changed_ignores_other_session(self) -> None:
        """归属过滤：他会话的 plan_mode_changed 不改本端状态栏（防污染）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event("plan_mode_changed", {"session_id": "sess-other", "is_plan_mode": True})
        assert shim.foreground_coara.is_plan_mode is False

    def test_session_started_clears_usage_snapshot(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event(
            "llm_turn_complete",
            {"usage": {"input_tokens": 50000, "output_tokens": 6000}, "cache_hit_ratio": 0.94},
        )
        snap = shim.foreground_coara._llm_usage_snapshot
        assert snap.has_reported_input is True
        transport.inject_event("session_started", {"session_id": "sess-new"})
        snap = shim.foreground_coara._llm_usage_snapshot
        assert snap.has_reported_input is False
        assert snap.usage is None or snap.usage == {}
        assert snap.cache_hit_ratio is None

    def test_workspace_switched_updates_mirror(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event(
            "workspace_switched",
            {"workspace_id": "ws-2", "workspace_name": "side", "workspace_dir": "D:/ws/side", "session_id": "sess-9"},
        )
        assert shim.workspace_manager.active_name == "side"
        assert shim.foreground_coara.workspace_dir == "D:/ws/side"
        assert shim.foreground_coara.session_id == "sess-9"
        assert shim._foreground_session_id == "ws-2"
        assert shim.foreground_coara.has_active_turn() is False
        assert shim.foreground_coara._continuation_inputs == []

    def test_turn_lifecycle_mirror(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        fg = shim.foreground_coara

        # web 来源回合：不驱动 CLI 本地活跃镜像（各端独立零同步）。
        transport.inject_event("user_message", {"source": "web", "session_id": "sess-1", "workspace_id": "ws-1"})
        assert fg.has_active_turn() is False
        assert fg._active_turn_source == ""
        assert shim._sessions["ws-1"].has_active_turn() is False

        # 本端来源回合：驱动活跃镜像。
        transport.inject_event(
            "user_message", {"source": "cli-attached", "session_id": "sess-1", "workspace_id": "ws-1"}
        )
        assert fg.has_active_turn() is True
        assert fg._active_turn_source == "cli-attached"
        assert shim.has_active_turn() is True
        assert shim._sessions["ws-1"].has_active_turn() is True

        transport.inject_event("completed", {"session_id": "sess-1", "workspace_id": "ws-1"})
        assert fg.has_active_turn() is False
        assert fg._active_turn_source == ""
        assert shim._sessions["ws-1"].has_active_turn() is False

    def test_turn_interrupted_clears_active(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event("llm_request_start", {"workspace_id": "ws-1", "source": "cli-attached"})
        assert shim.foreground_coara.has_active_turn() is True
        transport.inject_event("turn_interrupted", {"workspace_id": "ws-1"})
        assert shim.foreground_coara.has_active_turn() is False

    def test_turn_active_ignores_other_attach_channel(self) -> None:
        """同空间另一 CLI 的回合事件带它连接 channel_id：本端不转 spinner。"""
        transport, shim = _make_shim()
        transport.inject({**_flat_attached_frame(), "channel_id": "conn-self"})
        assert shim._attach_channel_id == "conn-self"

        transport.inject_event(
            "llm_request_start",
            {
                "workspace_id": "ws-1",
                "source": "cli-attached",
                "channel_id": "conn-other",
            },
        )
        assert shim.foreground_coara.has_active_turn() is False

        transport.inject_event(
            "llm_request_start",
            {
                "workspace_id": "ws-1",
                "source": "cli-attached",
                "channel_id": "conn-self",
            },
        )
        assert shim.foreground_coara.has_active_turn() is True

        transport.inject_event(
            "completed",
            {"workspace_id": "ws-1", "channel_id": "conn-other"},
        )
        assert shim.foreground_coara.has_active_turn() is True

        transport.inject_event(
            "completed",
            {"workspace_id": "ws-1", "channel_id": "conn-self"},
        )
        assert shim.foreground_coara.has_active_turn() is False

    def test_usage_snapshot_from_llm_turn_complete(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event(
            "llm_turn_complete",
            {
                "llm_output": {
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "finish_reason": "stop",
                },
                "session_id": "sess-1",
            },
        )
        snap = shim.foreground_coara._llm_usage_snapshot
        assert snap.has_reported_input is True
        assert snap.usage["input_tokens"] == 100
        assert snap.usage["output_tokens"] == 20
        assert snap.cumulative_prompt_tokens == 100

    def test_usage_snapshot_from_llm_turn_complete_top_level_fallback(self) -> None:
        """Legacy / test payloads may still put usage at top level."""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event(
            "llm_turn_complete",
            {"usage": {"input_tokens": 50, "output_tokens": 10}, "cache_hit_ratio": 0.5},
        )
        snap = shim.foreground_coara._llm_usage_snapshot
        assert snap.has_reported_input is True
        assert snap.usage["input_tokens"] == 50
        assert snap.cumulative_prompt_tokens == 50

    def test_usage_snapshot_accumulates_across_turns(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        for inp, out in ((100, 20), (150, 30)):
            transport.inject_event(
                "llm_turn_complete",
                {"llm_output": {"usage": {"input_tokens": inp, "output_tokens": out}, "finish_reason": "stop"}},
            )
        snap = shim.foreground_coara._llm_usage_snapshot
        assert snap.usage["input_tokens"] == 150
        assert snap.usage["output_tokens"] == 30
        assert snap.cumulative_prompt_tokens == 250

    def test_normalize_snapshot_raw_usage_marks_reported_input(self) -> None:
        """服务端 usage 帧是原始 provider dict（input_tokens 等）：has_reported_input
        由 total_prompt_tokens 判定（不再依赖摊平的 context_tokens），context 数字不恒 0。"""
        from src.cli.root_shim import _normalize_snapshot
        from src.llm.usage import total_prompt_tokens

        frame = _snapshot()
        frame["usage"] = {
            "input_tokens": 208460,
            "output_tokens": 760,
            "cached_tokens": 207872,
            "cache_hit_ratio": 0.99,
        }
        normalized = _normalize_snapshot(frame)
        mirror_data = normalized["foreground"]["usage_snapshot"]
        assert mirror_data["has_reported_input"] is True
        assert mirror_data["usage"]["input_tokens"] == 208460
        # spinner 口径：total_prompt_tokens + output_tokens（cache 命中不计入增量 prompt）
        assert total_prompt_tokens(mirror_data["usage"]) + int(mirror_data["usage"].get("output_tokens") or 0) > 0

    def test_normalize_snapshot_zero_usage_not_reported(self) -> None:
        from src.cli.root_shim import _normalize_snapshot

        frame = _snapshot()
        frame["usage"] = {"input_tokens": 0, "cache_hit_ratio": None}
        assert _normalize_snapshot(frame)["foreground"]["usage_snapshot"]["has_reported_input"] is False

    def test_continuation_queue_mirror(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        fg = shim.foreground_coara

        transport.inject_event("continuation_input_received", {"text": "跟话一", "source": "cli"})
        # web 来源跟话：不进 CLI 排队镜像（各端显示独立）。
        transport.inject_event("continuation_input_received", {"text": "跟话二", "source": "web"})
        assert [item.text for item in fg._continuation_inputs] == ["跟话一"]
        assert isinstance(fg._continuation_inputs[0], ContinuationInput)

        transport.inject_event("continuation_input_injected", {"user_texts": ["跟话一"]})
        assert fg._continuation_inputs == []

        transport.inject_event("continuation_input_injected", {})
        assert fg._continuation_inputs == []


class TestActionPassthrough:
    @pytest.mark.asyncio
    async def test_interrupt_frame_format(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event("llm_request_start", {"workspace_id": "ws-1", "source": "cli-attached"})
        await _flush()
        assert shim.foreground_coara.has_active_turn() is True

        ok = shim.foreground_coara.interrupt_current_turn("user_ctrl_c", interrupt_source="cli_prompt_ctrl_c")
        await _flush()

        assert ok is True
        frame = transport.sent[-1]
        # 客户端细粒度来源经 interrupt 帧透传到内核（服务端不再覆盖为 attach_client）。
        assert frame == {"type": "interrupt", "reason": "user_ctrl_c", "source": "cli_prompt_ctrl_c"}
        assert shim.foreground_coara.has_active_turn() is False

    @pytest.mark.asyncio
    async def test_submit_continuation_input_frame(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await shim.foreground_coara.submit_continuation_input("中途补充", source="cli")

        frame = transport.sent[-1]
        # 服务端收 type=continuation，固定记 source="cli-attached"。
        assert frame == {"type": "continuation", "text": "中途补充", "image_blocks": []}
        assert [i.text for i in shim.foreground_coara._continuation_inputs] == ["中途补充"]

    @pytest.mark.asyncio
    async def test_cancel_queued_continuation_frame(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event("continuation_input_received", {"text": "跟话一"})
        await _flush()

        ok = await shim.foreground_coara.cancel_queued_continuation("跟话一")

        assert ok is True
        assert transport.sent[-1] == {"type": "cancel_continuation", "text": "跟话一"}
        # RPC 化后本地镜像由 Esc handler 经 pop_latest_queued_followup 删除，
        # cancel_queued_continuation 本身不再动本地镜像（防双删）。
        assert [i.text for i in shim.foreground_coara._continuation_inputs] == ["跟话一"]

    @pytest.mark.asyncio
    async def test_drain_continuation_inputs(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event("continuation_input_received", {"text": "跟话一"})
        transport.inject_event("continuation_input_received", {"text": "跟话二"})

        drained = await shim.foreground_coara.drain_continuation_inputs()

        assert transport.sent[-1] == {"type": "drain_continuation"}
        assert [item.text for item in drained] == ["跟话一", "跟话二"]
        assert all(isinstance(item, ContinuationInput) for item in drained)
        assert drained[0].text == "跟话一"
        assert drained[0].image_blocks is None
        assert shim.foreground_coara._continuation_inputs == []

    def test_record_user_activity_is_local_noop(self) -> None:
        """服务端无 user_activity 类型（chat 到达即刷新活动计时）：客户端不发帧；
        且与老 CLI 契约对齐是同步方法（界面直接调用，async 会 RuntimeWarning）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        sent_before = len(transport.sent)
        shim.record_user_activity()
        assert len(transport.sent) == sent_before

    @pytest.mark.asyncio
    async def test_execute_command_roundtrip(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        async def reply() -> None:
            await asyncio.sleep(0)
            # 服务端回执不回显 request_id。
            transport.inject(
                {
                    "type": "command_result",
                    "result": {"output": "已切换模型", "action": "none", "data": {}, "exit_session": False},
                }
            )

        task = asyncio.create_task(shim.execute_command("/model kimi"))
        await reply()
        result = await task

        command_frame = next(f for f in transport.sent if f["type"] == "command")
        assert command_frame["type"] == "command"
        assert command_frame["text"] == "/model kimi"
        assert command_frame["request_id"]  # 客户端生成 uuid 随帧发
        assert result.output == "已切换模型"
        assert result.action == "none"
        assert result.exit_session is False

    @pytest.mark.asyncio
    async def test_switch_workspace_command_updates_toolbar_chrome(self) -> None:
        """/ws switch 回执须立刻刷新状态栏字段（不等 workspace_switched 事件）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()
        assert shim.workspace_manager.active_name == "main"
        assert shim.foreground_coara.provider_name == "deepseek"

        async def reply() -> None:
            await asyncio.sleep(0)
            transport.inject(
                {
                    "type": "command_result",
                    "result": {
                        "output": "已切换到工作空间 nx",
                        "action": "switch_workspace",
                        "data": {
                            "name": "nx",
                            "workspace_id": "ws-nx",
                            "workspace_dir": "D:/ws/nx",
                            "session_id": "sess-nx",
                            "coara_id": "root-nx",
                            "provider_name": "openai",
                            "model_name": "gpt-4o",
                            "is_plan_mode": False,
                        },
                        "exit_session": False,
                    },
                }
            )
            await _flush()

        task = asyncio.create_task(shim.execute_command("/ws switch nx"))
        await reply()
        result = await task
        assert result.action == "switch_workspace"
        assert shim.workspace_manager.active_name == "nx"
        assert shim._foreground_session_id == "ws-nx"
        assert shim.foreground_coara.workspace_dir == "D:/ws/nx"
        assert shim.foreground_coara.session_id == "sess-nx"
        assert shim.foreground_coara.provider_name == "openai"
        assert shim.foreground_coara.model_name == "gpt-4o"
        assert shim.foreground_active_name() == "nx"

    @pytest.mark.asyncio
    async def test_switch_workspace_updates_coara_id(self) -> None:
        """/ws switch 回执须刷新 identity.coara_id，否则新空间 completed 被旧 id 误滤。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()
        assert shim.identity.coara_id == "root-1"
        assert shim.foreground_coara.identity.coara_id in {"root-1", "fg-1"}

        async def reply() -> None:
            await asyncio.sleep(0)
            transport.inject(
                {
                    "type": "command_result",
                    "result": {
                        "output": "已切换到工作空间 nx",
                        "action": "switch_workspace",
                        "data": {
                            "name": "nx",
                            "workspace_id": "ws-nx",
                            "workspace_dir": "D:/ws/nx",
                            "session_id": "sess-nx",
                            "coara_id": "root-nx",
                            "provider_name": "openai",
                            "model_name": "gpt-4o",
                        },
                        "exit_session": False,
                    },
                }
            )
            await _flush()

        task = asyncio.create_task(shim.execute_command("/ws switch nx"))
        await reply()
        await task
        assert shim.identity.coara_id == "root-nx"
        assert shim.foreground_coara.identity.coara_id == "root-nx"

    @pytest.mark.asyncio
    async def test_late_thinking_after_stream_end_does_not_revive_spinner(self) -> None:
        """本地流收尾后迟到的 thinking_progress 不得再点亮 has_active_turn。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()
        fg = shim.foreground_coara

        async def consume() -> list[str]:
            chunks: list[str] = []
            async for chunk in fg.process_message("hi", source="cli-attached"):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "chunk", "text": "ok"})
        transport.inject({"type": "turn_end", "reason": "complete"})
        await task
        assert fg.has_active_turn() is False

        transport.inject_event(
            "thinking_progress",
            {"source": "cli-attached", "session_id": "sess-1", "workspace_id": "ws-1"},
        )
        assert fg.has_active_turn() is False
        assert fg._suppress_trace_turn_active is True

    def test_turn_end_accepts_switched_coara_id(self) -> None:
        """切空间后新 coara_id 的 completed 须能清掉活跃镜像。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        shim.identity.coara_id = "root-nx"
        shim.foreground_coara.identity.coara_id = "root-nx"
        fg = shim.foreground_coara
        fg._suppress_trace_turn_active = False
        transport.inject(
            {
                "type": "event",
                "event_type": "llm_request_start",
                "coara_id": "root-nx",
                "coara_name": "Coara",
                "message": "",
                "payload": {"source": "cli-attached", "workspace_id": "ws-nx"},
            }
        )
        assert fg.has_active_turn() is True
        transport.inject(
            {
                "type": "event",
                "event_type": "completed",
                "coara_id": "root-nx",
                "coara_name": "Coara",
                "message": "",
                "payload": {"workspace_id": "ws-nx"},
            }
        )
        assert fg.has_active_turn() is False

    def test_workspace_switched_event_updates_model_chrome(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        transport.inject_event(
            "workspace_switched",
            {
                "end": "cli",
                "workspace_name": "side",
                "workspace_id": "ws-2",
                "workspace_dir": "D:/ws/side",
                "session_id": "sess-2",
                "coara_id": "root-side",
                "provider_name": "anthropic",
                "model_name": "claude-sonnet",
                "is_plan_mode": True,
                "previous_workspace_id": "ws-1",
            },
        )
        assert shim.workspace_manager.active_name == "side"
        assert shim.foreground_coara.provider_name == "anthropic"
        assert shim.foreground_coara.model_name == "claude-sonnet"
        assert shim.foreground_coara.is_plan_mode is True
        assert shim.foreground_coara.session_id == "sess-2"
        assert shim.identity.coara_id == "root-side"
        assert shim.foreground_coara.identity.coara_id == "root-side"

    @pytest.mark.asyncio
    async def test_chunk_desk_prefix_once(self) -> None:
        """服务台投递：首块正文带 [daily] 台签，后续块不再重复。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        await _flush()
        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("@daily hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "turn_start", "turn_id": "t-desk", "source": "cli-attached", "desk": "daily"})
        transport.inject(
            {"type": "chunk", "turn_id": "t-desk", "text": "第一段", "desk": "daily", "source": "cli-attached"}
        )
        transport.inject(
            {"type": "chunk", "turn_id": "t-desk", "text": "第二段", "desk": "daily", "source": "cli-attached"}
        )
        transport.inject({"type": "turn_end", "turn_id": "t-desk", "reason": "complete"})
        await task
        assert chunks[0] == "[daily] 第一段"
        assert chunks[1] == "第二段"


class TestProcessMessageStreaming:
    @pytest.mark.asyncio
    async def test_stream_turn_chunks_and_end(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("你好", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)

        chat_frame = transport.sent[-1]
        # 服务端收 text/image_blocks（无 source/turn_id——服务端生成并回显）。
        assert chat_frame == {"type": "chat", "text": "你好", "image_blocks": []}
        server_turn_id = "srv-turn-1"

        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 2, "text": "你"})
        transport.inject({"type": "chunk", "turn_id": server_turn_id, "seq": 3, "text": "好"})
        transport.inject({"type": "turn_end", "turn_id": server_turn_id, "seq": 4, "reason": "complete"})
        await task

        assert chunks == ["你", "好"]

    @pytest.mark.asyncio
    async def test_stream_turn_with_images(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        images = [{"type": "image", "data": "base64..."}]

        async def consume() -> None:
            async for _ in shim.foreground_coara.process_message("看图", image_blocks=images, source="cli"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        chat_frame = transport.sent[-1]
        assert chat_frame["image_blocks"] == images
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "complete"})
        await task

    @pytest.mark.asyncio
    async def test_stream_turn_error_yields_message(self) -> None:
        """turn_end reason=error 时把文案当最后一条 chunk，不抛 RuntimeError。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "error", "message": "provider 500"})
        await task
        assert chunks == ["Error: provider 500"]

    @pytest.mark.asyncio
    async def test_stream_turn_interrupt_does_not_yield_error_chunk(self) -> None:
        """Ctrl+C 本地收尾不得显示 Error: interrupted（冲掉原打断体验）。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        shim.foreground_coara.interrupt_current_turn("user")
        await task
        assert chunks == []
        assert not any("Error:" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_stream_turn_error_reason_only_gets_friendly_fallback(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "error"})
        await task
        assert len(chunks) == 1
        assert chunks[0].startswith("Error:")
        assert chunks[0].lower().removeprefix("error:").strip() != "error"

    @pytest.mark.asyncio
    async def test_stream_turn_accepts_tool_frame(self) -> None:
        """✓/✗ 工具行走独立 tool 帧：客户端必须路由到回合流，按 chunk 输出。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("改文件", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "tool", "turn_id": "srv-turn-1", "text": "✓ edit(`a.txt`)"})
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "complete"})
        await task

        assert chunks == ["✓ edit(`a.txt`)"]

    @pytest.mark.asyncio
    async def test_stream_turn_diff_frame_invokes_callback(self) -> None:
        """diff 帧须到达 _diff_callback（display.queue_diff_frame），不能入口丢弃。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        received: list[dict[str, Any]] = []
        shim._diff_callback = lambda frame: received.append(frame)

        async def consume() -> None:
            async for _ in shim.foreground_coara.process_message("改文件", source="cli"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject(
            {
                "type": "diff",
                "turn_id": "srv-turn-1",
                "display_blocks": [{"kind": "diff", "lines": ["+x"]}],
                "tool_name": "edit",
            }
        )
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "complete"})
        await task

        assert len(received) == 1
        assert received[0]["type"] == "diff"
        assert received[0]["display_blocks"] == [{"kind": "diff", "lines": ["+x"]}]

    @pytest.mark.asyncio
    async def test_chunk_without_turn_id_routes_to_single_stream(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "chunk", "text": "无id chunk"})
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "complete"})
        await task
        assert chunks == ["无id chunk"]

    @pytest.mark.asyncio
    async def test_server_turn_id_binding_via_turn_start(self) -> None:
        """服务端回显的 turn_id 经 turn_start 帧绑定最新发起流，后续帧按归属路由。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in shim.foreground_coara.process_message("hi", source="cli"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "turn_start", "turn_id": "srv-turn-9", "source": "cli-attached"})
        transport.inject({"type": "chunk", "turn_id": "srv-turn-9", "text": "归属chunk"})
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-9", "reason": "complete"})
        await task
        assert chunks == ["归属chunk"]

    @pytest.mark.asyncio
    async def test_disconnect_ends_active_turn(self) -> None:
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})

        async def consume() -> None:
            async for _ in shim.foreground_coara.process_message("hi", source="cli"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        transport.inject({"type": "connection_state", "connected": False, "reason": "disconnected"})
        # 断连语义已从「抛 RuntimeError」改为「软失败交付一行 Error 文案」
        # （root_shim._stream_turn 的 end 路径软失败化），任务应正常收尾不抛。
        await task
        assert task.done() and task.exception() is None

    @pytest.mark.asyncio
    async def test_active_turn_survives_early_completed_trace(self) -> None:
        """Ctrl+C 误退根因回归：本地回合流存活时，服务端 completed trace 事件
        先行到达（清 _active_turn 镜像）不得让 has_active_turn 判成空闲——
        Ctrl+C 判定以本地回合流（turn_end 帧未到=还在跑）为权威，防回合进行
        中按 Ctrl+C 误退出 CLI。
        """
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        fg = shim.foreground_coara

        async def consume() -> None:
            async for _ in fg.process_message("hi", source="cli"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        # 本地回合流已建（chat 帧已发、turn_end 未到）→ has_active_turn 为真
        assert fg.has_active_turn() is True
        # 服务端 completed trace 先行到达：旧实现会立即清 _active_turn → 空闲
        transport.inject_event("completed", {"session_id": "sess-1", "workspace_id": "ws-1"})
        await asyncio.sleep(0)
        # 本地回合流仍存活：判定必须保持活跃（Ctrl+C 应打断而非退出）
        assert fg.has_active_turn() is True
        # turn_end 帧真正到达 → 流收尾 → 判定回空闲
        transport.inject({"type": "turn_end", "turn_id": "srv-turn-1", "reason": "complete"})
        await task
        assert fg.has_active_turn() is False


class TestEventLoopScheduling:
    @pytest.mark.asyncio
    async def test_frames_from_foreign_thread_dispatch_in_loop(self) -> None:
        """事件循环内创建的 shim：注入帧经 call_soon_threadsafe 转入循环应用。"""
        transport, shim = _make_shim()
        transport.inject({"type": "attached", "snapshot": _snapshot()})
        # 同步注入后尚未应用（call_soon_threadsafe 排队中）
        assert shim.ready is False
        await _flush()
        assert shim.ready is True
        assert shim.foreground_coara.session_id == "sess-1"


class TestApprovalFrameRouting:
    @pytest.mark.asyncio
    async def test_approval_request_frame_reaches_callback(self) -> None:
        """shim 须处理 approval_request，不能只认旧名 approval。"""
        transport, shim = _make_shim()
        seen: list[dict[str, Any]] = []
        shim._approval_callback = seen.append  # type: ignore[method-assign]
        transport.inject(
            {
                "type": "approval_request",
                "approval_id": "ap-1",
                "question": "允许执行？",
                "options": [{"label": "同意"}, {"label": "不同意"}],
            }
        )
        await _flush()
        assert len(seen) == 1
        assert seen[0]["approval_id"] == "ap-1"
        assert seen[0]["question"] == "允许执行？"

    @pytest.mark.asyncio
    async def test_legacy_approval_frame_still_accepted(self) -> None:
        transport, shim = _make_shim()
        seen: list[dict[str, Any]] = []
        shim._approval_callback = seen.append  # type: ignore[method-assign]
        transport.inject({"type": "approval", "approval_id": "ap-legacy", "question": "q"})
        await _flush()
        assert len(seen) == 1
        assert seen[0]["approval_id"] == "ap-legacy"


class TestCommandTimeoutBudget:
    """/compact 这类慢命令：等它的时间要够长，且超时文案不能谎报「内核无响应」。"""

    def test_slow_command_gets_longer_budget(self) -> None:
        from src.cli.root_shim import _command_name, _command_timeout, _is_slow_command

        assert _is_slow_command("/compact")
        assert _is_slow_command("  /compact  ")
        assert not _is_slow_command("/status")
        assert _command_name(" /Compact ") == "compact"
        assert _command_name("普通文本") == ""
        assert _command_timeout("/compact") > _command_timeout("/status")

    @pytest.mark.asyncio
    async def test_slow_command_timeout_reports_still_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.cli.root_shim._SLOW_COMMAND_TIMEOUT_S", 0.01)
        transport, shim = _make_shim()
        result = await shim.execute_command("/compact")
        # 命令确实发给了内核（不是端上直接放弃）
        assert transport.sent[-1]["type"] == "command"
        assert transport.sent[-1]["text"] == "/compact"
        assert "仍在执行" in result.output
        assert "内核无响应" not in result.output

    @pytest.mark.asyncio
    async def test_plain_command_timeout_keeps_old_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.cli.root_shim._COMMAND_TIMEOUT_S", 0.01)
        _transport, shim = _make_shim()
        result = await shim.execute_command("/status")
        assert result.output == "命令回执超时（内核无响应）"


class TestSlowCommandSpinner:
    """/compact 等待期必须转 spinner（内核压缩期间不回帧，屏幕不该静默）。"""

    def test_slow_command_label_mapping(self) -> None:
        from src.cli.attached_chat_runner import _slow_command_label

        assert _slow_command_label("/compact") == "正在压缩会话历史…"
        assert _slow_command_label("  /compact ") == "正在压缩会话历史…"
        assert _slow_command_label("/status") == ""
        assert _slow_command_label("普通文本") == ""

    @pytest.mark.asyncio
    async def test_slow_command_spins_while_waiting(self) -> None:
        from pathlib import Path

        import src.cli.attached_chat_runner as runner
        from src.cli.root_shim import CommandResult

        events: list[str] = []

        class _FakeSpinner:
            def begin_local_wait(self, label: str) -> None:
                events.append(f"begin:{label}")

            def end_local_wait(self) -> None:
                events.append("end")

        class _FakeRoot:
            async def execute_command(self, command: str) -> Any:
                events.append("exec")
                return CommandResult(output="ok", action="none")

        should_exit = await runner.handle_attached_chat_command(
            _FakeRoot(), "/compact", Path("D:/ws"), None, _FakeSpinner()
        )
        assert should_exit is False
        assert events == ["begin:正在压缩会话历史…", "exec", "end"]

    @pytest.mark.asyncio
    async def test_fast_command_does_not_spin(self) -> None:
        from pathlib import Path

        import src.cli.attached_chat_runner as runner
        from src.cli.root_shim import CommandResult

        events: list[str] = []

        class _FakeSpinner:
            def begin_local_wait(self, label: str) -> None:
                events.append("begin")

            def end_local_wait(self) -> None:
                events.append("end")

        class _FakeRoot:
            async def execute_command(self, command: str) -> Any:
                return CommandResult(output="ok", action="none")

        await runner.handle_attached_chat_command(_FakeRoot(), "/status", Path("D:/ws"), None, _FakeSpinner())
        assert events == []
