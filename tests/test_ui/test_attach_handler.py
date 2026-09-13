"""Integration tests for the attach (/ws/attach) server-side handler.

覆盖握手校验、双向占用（attach 侧）、断连释放。root 用 MagicMock 轻量替身，
WebServer 经 __new__ 绕过 __init__ 只注入 handler 依赖的属性。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from src.ui.attach_registry import AttachRegistry
from src.ui.web_server import WebServer


def _make_ws(*, close_code: int | None = None) -> AsyncMock:
    ws = AsyncMock(spec=web.WebSocketResponse)
    ws.closed = False
    ws.close_code = close_code
    ws.prepare = AsyncMock()
    ws.close = AsyncMock()
    ws.send_str = AsyncMock()
    ws.receive_json = AsyncMock()
    return ws


@pytest.fixture
def patch_ws_ctor(monkeypatch: pytest.MonkeyPatch):
    """handler 内部 new WebSocketResponse 并 prepare(request)——真实握手要真
    request。测试里 patch 构造器返回预置 mock ws，绕过握手。用法：
    ws = _make_ws(); patch_ws_ctor(ws); await server._handle_attach_websocket(...)"""
    import src.ui.web_server as ws_mod

    holder: dict = {}

    def _install(ws: AsyncMock) -> None:
        monkeypatch.setattr(ws_mod.web, "WebSocketResponse", lambda **kwargs: ws)

    holder["install"] = _install
    return holder


def _make_server(*, foreground_id: str | None, entry: SimpleNamespace | None) -> WebServer:
    """构造一个只够 attach handler 跑的 WebServer 替身。"""
    server = WebServer.__new__(WebServer)
    server.attach_registry = AttachRegistry()
    server._chat_tasks = {}
    server._module_roots = {}
    server.interaction_channel = MagicMock()
    server.interaction_channel.cancel_pending_for_connection = MagicMock()
    # attach 专属审批通道：真实实例（registry 用真 AttachRegistry，send_to 经
    # mock ws 捕获帧）；回合 turn() 与 approval_reply 路由都依赖它。
    from src.ui.attach_interaction_channel import AttachRemoteInteractionChannel

    server.attach_interaction_channel = AttachRemoteInteractionChannel(server)

    root = MagicMock()
    root._foreground_session_id = foreground_id
    root._cli_view_workspace_id = foreground_id
    root._sessions = {}
    root.workspace_manager = MagicMock() if entry is not None else None
    if entry is not None:
        root.workspace_manager.registry.resolve_name_or_id = MagicMock(return_value=entry)
        root.workspace_manager.registry.get_by_id = MagicMock(return_value=entry)
    # ensure_workspace_session 返回带 coara.session_id 的 session
    session = SimpleNamespace(coara=SimpleNamespace(session_id="sess-1"))
    root.ensure_workspace_session = AsyncMock(return_value=session)
    root.record_user_activity = MagicMock()
    root.set_view_workspace = AsyncMock(return_value=True)
    server.root = root
    # token 校验：直接放行（测握手/占用，不测鉴权）
    server._check_token = MagicMock()
    return server


async def _register_pin(server: WebServer, ws: AsyncMock, conn_id: str = "c1", workspace_id: str = "ws-a") -> None:
    """测试用：预先占用 pin，使 _handle_attach_message 能从 conn 读 workspace_id。"""
    conn = await server.attach_registry.register(ws, conn_id, workspace_id)
    assert conn is not None
    if workspace_id not in server.root._sessions:
        server.root._sessions[workspace_id] = SimpleNamespace(
            coara=SimpleNamespace(
                session_id="sess-1",
                interrupt_current_turn=MagicMock(),
                submit_continuation_input=MagicMock(),
                drain_continuation_inputs=MagicMock(return_value=[]),
                _continuation_inputs=[],
                _continuation_event=None,
            )
        )


def _entry(ws_id: str = "ws-a", name: str = "alpha") -> SimpleNamespace:
    return SimpleNamespace(id=ws_id, name=name)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["/exit", "/quit", "/login", "/status", "/ws", "/new"])
async def test_web_command_blocklist_rejects_graphical_equivalents(blocked: str) -> None:
    """Web 端斜杠黑名单：图形等价命令被拦（点鼠标即可完成）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    ws = _make_ws()
    await server._handle_command({"type": "command", "text": blocked}, ws)
    frames = _sent_frames(ws)
    err = [f for f in frames if f.get("type") == "error"]
    assert err, f"{blocked} 应被 Web 黑名单拦截"
    assert "界面操作" in err[0]["message"]


@pytest.mark.asyncio
async def test_web_command_result_stamps_workspace_boundary() -> None:
    """Web /compact 回执必须盖 workspace_dir/session_id，否则端上边界守卫会整帧丢掉。"""
    from pathlib import Path

    server = _make_server(foreground_id="ws-a", entry=_entry())
    ws_dir = Path("D:/ws/demo")
    coara = SimpleNamespace(
        session_id="sess-compact",
        workspace_dir=ws_dir,
        has_active_turn=MagicMock(return_value=False),
        _process_lock=SimpleNamespace(locked=MagicMock(return_value=False)),
        message_history=[object()],
        _compress_inflight=False,
    )
    server.root.foreground_coara = coara
    server.root.resolve_workspace_coara = MagicMock(return_value=coara)
    server._view_coara = MagicMock(return_value=coara)
    server._view_store = None
    server.workspace_dir = ws_dir
    ws = _make_ws()

    async def _fake_execute(root, raw, target_coara=None, origin_source="", interaction_channel=None):
        return SimpleNamespace(
            output="已压缩（LLM 摘要）：10 → 3 条消息",
            action=None,
            data={"compressed": True, "info": {"original_count": 10, "compressed_count": 3}},
            exit_session=False,
        )

    import src.ui.web_server as ws_mod

    orig = ws_mod.execute_command
    ws_mod.execute_command = _fake_execute
    try:
        await server._handle_command(
            {"type": "command", "text": "/compact", "workspace_dir": str(ws_dir)},
            ws,
        )
    finally:
        ws_mod.execute_command = orig

    frames = _sent_frames(ws)
    result = [f for f in frames if f.get("type") == "command_result"]
    assert result, "/compact 应回 command_result"
    frame = result[0]
    assert frame.get("workspace_dir") == str(ws_dir)
    assert frame.get("session_id") == "sess-compact"
    assert "已压缩" in frame["result"]["output"]


def _sent_frames(ws: AsyncMock) -> list[dict]:
    return [json.loads(call.args[0]) for call in ws.send_str.await_args_list]


@pytest.mark.asyncio
async def test_handshake_success_sends_attached_and_occupies(patch_ws_ctor) -> None:
    server = _make_server(foreground_id="ws-other", entry=_entry())
    ws = _make_ws()
    ws.receive_json.return_value = {"type": "attach", "workspace": "alpha"}
    # 握手成功后立即结束消息循环
    ws.__aiter__.return_value = iter([])
    patch_ws_ctor["install"](ws)

    await server._handle_attach_websocket(MagicMock())

    frames = _sent_frames(ws)
    attached = [f for f in frames if f.get("type") == "attached"]
    assert attached and attached[0]["workspace"] == "alpha"
    assert attached[0]["session"] == "sess-1"
    # 握手不得改全局 cli view（多 attach / stale 重开）
    server.root.set_view_workspace.assert_not_awaited()
    # RootShim 快照字段存在（替身属性为空值也不阻塞握手）
    for field in (
        "session_id",
        "workspace_dir",
        "coara_id",
        "agent_name",
        "provider_name",
        "model_name",
        "is_plan_mode",
        "tools_count",
        "skills_count",
        "active_name",
        "foreground_session_id",
        "workspaces",
        "usage",
        "context_window",
        "turn_source",
        "service_desks",
    ):
        assert field in attached[0], f"attached 帧缺字段 {field}"
    # 断连（消息循环结束）即释放占用
    assert server.attach_registry.is_workspace_occupied("ws-a") is False


@pytest.mark.asyncio
async def test_handshake_rejects_bad_first_message(patch_ws_ctor) -> None:
    server = _make_server(foreground_id="ws-other", entry=_entry())
    ws = _make_ws()
    ws.receive_json.return_value = {"type": "chat", "text": "hi"}
    patch_ws_ctor["install"](ws)

    await server._handle_attach_websocket(MagicMock())

    ws.close.assert_awaited()
    assert ws.close.await_args.kwargs.get("code") == 4002


@pytest.mark.asyncio
async def test_handshake_rejects_unknown_workspace(patch_ws_ctor) -> None:
    server = _make_server(foreground_id="ws-other", entry=_entry())
    server.root.workspace_manager.registry.resolve_name_or_id = MagicMock(return_value=None)
    ws = _make_ws()
    ws.receive_json.return_value = {"type": "attach", "workspace": "ghost"}
    patch_ws_ctor["install"](ws)

    await server._handle_attach_websocket(MagicMock())

    assert ws.close.await_args.kwargs.get("code") == 4004


@pytest.mark.asyncio
async def test_attach_allowed_on_root_foreground(patch_ws_ctor) -> None:
    """内核化后 CLI 一律 attach：可挂 root 前台空间（与 Web/手机视图并存）。"""
    server = _make_server(foreground_id="ws-a", entry=_entry(ws_id="ws-a"))
    ws = _make_ws()
    ws.receive_json.return_value = {"type": "attach", "workspace": "alpha"}
    ws.__aiter__.return_value = iter([])
    patch_ws_ctor["install"](ws)

    await server._handle_attach_websocket(MagicMock())

    frames = _sent_frames(ws)
    attached = [f for f in frames if f.get("type") == "attached"]
    assert attached and attached[0]["workspace"] == "alpha"
    assert not any(f.get("reason") == "occupied" for f in frames)
    # 消息循环结束即释放占用
    assert server.attach_registry.is_workspace_occupied("ws-a") is False


@pytest.mark.asyncio
async def test_attach_allows_second_connection_on_same_workspace(patch_ws_ctor) -> None:
    """同空间已有另一 attach 时，新连接仍握手成功（多终端）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry(ws_id="ws-a"))
    holder = _make_ws()
    await server.attach_registry.register(holder, "other-conn", "ws-a")

    ws = _make_ws()
    ws.receive_json.return_value = {"type": "attach", "workspace": "alpha"}
    ws.__aiter__.return_value = iter([])
    patch_ws_ctor["install"](ws)
    await server._handle_attach_websocket(MagicMock())

    frames = _sent_frames(ws)
    attached = [f for f in frames if f.get("type") == "attached"]
    assert attached and attached[0]["workspace"] == "alpha"
    assert not any(f.get("reason") == "occupied" for f in frames)
    assert "other-conn" in server.attach_registry.workspace_owners("ws-a")
    # 新连接在消息循环结束后会 unregister；holder 仍在
    assert server.attach_registry.workspace_owner("ws-a") == "other-conn" or (
        "other-conn" in server.attach_registry.workspace_owners("ws-a")
    )


@pytest.mark.asyncio
async def test_resume_handshake_redelivers_pending_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """重连二次握手（resume）按同一 approval_id 重发挂起审批帧——断连不取消、弹窗不丢。"""
    rebound: list[tuple[str, str]] = []

    class _FakeCenter:
        def get(self, approval_id: str) -> MagicMock:
            return MagicMock(state="pending")

        def rebind_reply_actor(self, approval_id: str, actor: str) -> bool:
            rebound.append((approval_id, actor))
            return True

    monkeypatch.setattr("src.coara.approval_center.get_approval_center", lambda: _FakeCenter())

    server = _make_server(foreground_id="ws-other", entry=_entry())
    old_ws = _make_ws()
    await _register_pin(server, old_ws, conn_id="conn-old", workspace_id="ws-a")
    await server.attach_interaction_channel.send_approval_request(
        {
            "approval_id": "ap-1",
            "question": "执行危险操作？",
            "options": [{"label": "同意", "description": ""}, {"label": "不同意", "description": ""}],
            "timeout_s": 300,
        },
        conn_id="conn-old",
    )
    # 断连：连接注销、挂起帧留着（对齐 web「断连只标记不取消」）
    await server.attach_registry.unregister("conn-old")
    server.attach_interaction_channel.mark_connection_disconnected("conn-old")
    assert server.attach_interaction_channel.pending_for_connection("conn-old")

    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-new", "ws-a")
    await server._handle_attach_message({"type": "attach", "resume": "conn-old"}, ws, "conn-new")

    frames = _sent_frames(ws)
    requests = [f for f in frames if f.get("type") == "approval_request"]
    assert [f["approval_id"] for f in requests] == ["ap-1"]
    assert requests[0]["question"] == "执行危险操作？"
    # 回执 actor 重绑到换回的 conn_id（否则重连后的答复被 Center 拒）
    assert rebound == [("ap-1", "conn-old")]
    assert "conn-old" in server.attach_registry._connections


def _bound_coara(**over) -> SimpleNamespace:
    base = {"session_id": "sess-1", "provider_name": "deepseek", "model_name": "chat"}
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_attach_compact_routes_to_bound_session() -> None:
    """/compact 走统一命令层且 target_coara 指向 pin 空间 session。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    bound = _bound_coara()
    server.root._sessions["ws-a"] = SimpleNamespace(coara=bound)
    ws = _make_ws()

    captured: dict = {}

    async def _fake_execute(root, raw, target_coara=None, origin_source="", interaction_channel=None):
        captured["raw"] = raw
        captured["target"] = target_coara
        captured["origin_source"] = origin_source
        captured["interaction_channel"] = interaction_channel
        return SimpleNamespace(output="ok", action=None, data={}, exit_session=False)

    import src.ui.web_server as ws_mod

    orig = ws_mod.execute_command
    ws_mod.execute_command = _fake_execute
    try:
        await _register_pin(server, ws)
        await server._handle_attach_command({"type": "command", "text": "/compact"}, ws, "c1")
    finally:
        ws_mod.execute_command = orig

    assert captured["raw"] == "/compact"
    assert captured["target"] is bound
    frames = _sent_frames(ws)
    assert any(f.get("type") == "command_result" for f in frames)


@pytest.mark.asyncio
async def test_attach_model_switch_uses_workspace_scoped_root_method() -> None:
    """/model <arg> 调 root.switch_llm_for_workspace（pin 空间），不打前台。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    server.root._sessions["ws-a"] = SimpleNamespace(coara=_bound_coara())
    server.root.switch_llm_for_workspace = MagicMock(return_value=("deepseek", "r1"))
    ws = _make_ws()

    import src.ui.web_server as ws_mod

    orig_resolve = ws_mod  # noqa: F841 - 占位，真实 patch 在下方 monkeypatch 风格
    # patch 模型目录解析，避免依赖真实 config
    import src.llm.model_catalog as catalog

    orig_sel = catalog.resolve_model_selection
    catalog.resolve_model_selection = lambda cm, arg, m: ("deepseek", "r1")
    try:
        await _register_pin(server, ws)
        await server._handle_attach_command({"type": "command", "text": "/model deepseek/r1"}, ws, "c1")
    finally:
        catalog.resolve_model_selection = orig_sel

    server.root.switch_llm_for_workspace.assert_called_once_with("ws-a", "deepseek", "r1", origin_source="cli-attached")
    frames = _sent_frames(ws)
    result = [f for f in frames if f.get("type") == "command_result"]
    assert result and "已切换" in result[0]["result"]["output"]


@pytest.mark.asyncio
async def test_attach_new_uses_workspace_scoped_semantics() -> None:
    """/new 走 start_new_session_for_workspace（pin 空间重开）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    coara = MagicMock()
    coara.interrupt_current_turn = MagicMock(return_value=True)
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    server.root.start_new_session_for_workspace = AsyncMock(return_value="new-id")

    ws = _make_ws()
    await _register_pin(server, ws)
    await server._handle_attach_command({"type": "command", "text": "/new"}, ws, "c1")
    server.root.start_new_session_for_workspace.assert_awaited_once_with(
        "ws-a", interrupt_source="cli_attached_new_command"
    )
    frames = _sent_frames(ws)
    result = [f for f in frames if f.get("type") == "command_result"]
    assert result and "new-id" in result[0]["result"]["output"]


@pytest.mark.asyncio
async def test_attach_command_passthrough_no_whitelist() -> None:
    """命令面已放开：原先被白名单拦截的命令现透传统一命令层（target_coara=pin 空间）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    bound = _bound_coara()
    server.root._sessions["ws-a"] = SimpleNamespace(coara=bound)

    for cmd in ("/status", "/usage", "/tools", "/thinking", "/help"):
        ws = _make_ws()
        captured: dict = {}

        async def _fake_execute(root, raw, target_coara=None, origin_source="", interaction_channel=None, _c=captured):
            _c["raw"] = raw
            _c["target"] = target_coara
            _c["origin_source"] = origin_source
            return SimpleNamespace(output=f"ran {raw}", action=None, data={}, exit_session=False)

        import src.ui.web_server as ws_mod

        orig = ws_mod.execute_command
        ws_mod.execute_command = _fake_execute
        try:
            await _register_pin(server, ws)
            await server._handle_attach_command({"type": "command", "text": cmd}, ws, "c1")
        finally:
            ws_mod.execute_command = orig

        assert captured.get("raw") == cmd, f"{cmd} 应透传统一命令层"
        assert captured.get("target") is bound
        assert captured.get("origin_source") == "cli-attached"
        frames = _sent_frames(ws)
        result = [f for f in frames if f.get("type") == "command_result"]
        assert result, f"{cmd} 应回 command_result"
        assert result[0]["result"]["output"] == f"ran {cmd}"


@pytest.mark.asyncio
async def test_interrupt_targets_bound_session() -> None:
    server = _make_server(foreground_id="ws-other", entry=_entry())
    coara = MagicMock()
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)

    await server._handle_attach_message({"type": "interrupt"}, ws, "c1")

    coara.interrupt_current_turn.assert_called_once()


@pytest.mark.asyncio
async def test_interrupt_uses_reason_from_message() -> None:
    """interrupt 透传 reason（客户端 Ctrl+C / stop 按钮来源不同）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    coara = MagicMock()
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)

    await server._handle_attach_message({"type": "interrupt", "reason": "user_ctrl_c"}, ws, "c1")

    coara.interrupt_current_turn.assert_called_once_with("user_ctrl_c", interrupt_source="attach_client")


@pytest.mark.asyncio
async def test_continuation_submits_to_bound_session() -> None:
    """continuation → 正式 TurnStream.user_message + submit_continuation_input。"""
    from src.coara.end_registry import EndRegistry

    server = _make_server(foreground_id="ws-other", entry=_entry())
    registry = EndRegistry()
    server.root.end_registry = registry
    # 触达惰性 _turns，供跟话建流写入
    assert server._turns == {}
    coara = MagicMock()
    coara.session_id = "sess-1"
    coara.has_active_turn = MagicMock(return_value=True)
    coara._active_turn = SimpleNamespace(turn_id="turn-live")
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)

    blocks = [{"type": "image", "data": "abc"}]
    await server._handle_attach_message({"type": "continuation", "text": "跟话", "image_blocks": blocks}, ws, "c1")

    coara.submit_continuation_input.assert_called_once_with("跟话", image_blocks=blocks, source="cli-attached")
    frames = _sent_frames(ws)
    assert frames and frames[-1].get("type") == "continuation_accepted"
    assert registry.sender_for("cli-attached", "sess-1") is not None
    followups = [
        s
        for s in server._turns.values()
        if str(getattr(getattr(s, "route", None), "channel_id", "") or "") == "c1"
    ]
    assert followups
    user_frames = [f for f in followups[0].buffer if f.get("type") == "user_message"]
    assert user_frames and user_frames[0].get("content") == "跟话"
    # fire-and-forget 广播：让 ensure_future 跑完再核对 WS
    await asyncio.sleep(0)
    assert any(f.get("type") == "user_message" and f.get("content") == "跟话" for f in _sent_frames(ws))


@pytest.mark.asyncio
async def test_continuation_rejects_empty() -> None:
    server = _make_server(foreground_id="ws-other", entry=_entry())
    coara = MagicMock()
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)

    await server._handle_attach_message({"type": "continuation", "text": "  "}, ws, "c1")

    coara.submit_continuation_input.assert_not_called()
    frames = _sent_frames(ws)
    assert any(f.get("type") == "error" for f in frames)


def _turn_end_event(
    *, turn_id: str = "", session_id: str = "", reason: str = "complete", origin_scope: str = "main_loop"
) -> Any:
    """内核主回合的 turn_end 事件（payload 形状同 TraceEmitter 打的那份）。"""
    from src.core.events import TraceEvent

    payload: dict[str, Any] = {"reason": reason, "origin_scope": origin_scope}
    if turn_id:
        payload["turn_id"] = turn_id
    if session_id:
        payload["session_id"] = session_id
    return TraceEvent(
        coara_id="c1",
        coara_name="考拉",
        event_type="turn_end",
        message="Turn completed",
        payload=payload,
    )


async def _make_followup_server() -> tuple[WebServer, AsyncMock, Any]:
    """建一次 attach 跟话：返回 (server, ws, 真实 EventBus)。

    跟话流经 _handle_attach_message 正常路径建立（turn-live / sess-1）；event_bus
    用真实实例——收尾链是「内核 publish turn_end → 订阅回调收流」，替身测不出
    订阅的 topic 是否正确。
    """
    from src.coara.end_registry import EndRegistry
    from src.coara.event_bus import EventBus

    server = _make_server(foreground_id="ws-other", entry=_entry())
    server.root.end_registry = EndRegistry()
    bus = EventBus()
    server.root.event_bus = bus
    server._subscriptions = []
    coara = MagicMock()
    coara.session_id = "sess-1"
    coara.has_active_turn = MagicMock(return_value=True)
    coara._active_turn = SimpleNamespace(turn_id="turn-live")
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)
    await server._handle_attach_message({"type": "continuation", "text": "跟话"}, ws, "c1")
    return server, ws, bus


def _turn_end_subscriber_count(bus: Any) -> int:
    return len(bus._sync_subscribers.get("turn_end", []))


def _followup_streams(server: WebServer) -> list[Any]:
    return [s for k, s in server._turns.items() if k.startswith("followup-attach-")]


@pytest.mark.asyncio
async def test_followup_stream_closed_on_turn_end() -> None:
    """跟话流在所属回合结束时收尾：turn_end + done，且收尾帧真的投回本连接。

    它没有自己的回合协程，不补这一手 done 恒 False → 回收不掉、每次重连都被当
    「在飞回合」回放、还长期留在活跃流候选里被误当投递目标。
    """
    server, ws, bus = await _make_followup_server()
    followups = _followup_streams(server)
    assert followups and not followups[0].done
    stream = followups[0]
    # 订阅是建流时幂等装上的（topic 精确 turn_end）
    assert _turn_end_subscriber_count(bus) == 1
    assert server._attach_turn_end_sub is not None

    bus.publish(_turn_end_event(turn_id="turn-live", session_id="sess-1"))

    assert stream.done
    ends = [f for f in stream.buffer if f.get("type") == "turn_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "complete"
    await asyncio.sleep(0)
    assert any(f.get("type") == "turn_end" for f in _sent_frames(ws)), "turn_end 应收尾帧投回本连接"

    # 幂等：同一事件重复到达不再追加帧、不再改状态
    bus.publish(_turn_end_event(turn_id="turn-live", session_id="sess-1"))
    assert len([f for f in stream.buffer if f.get("type") == "turn_end"]) == 1

    # 二次跟话不重复订阅
    await server._handle_attach_message({"type": "continuation", "text": "再来一条"}, ws, "c1")
    assert _turn_end_subscriber_count(bus) == 1

    # 服务重启（stop 注销订阅并清空登记）后再跟话：订阅重装，收尾链不断
    for sub in list(server._subscriptions):
        sub.unsubscribe()
    server._subscriptions.clear()
    await server._handle_attach_message({"type": "continuation", "text": "重启后跟话"}, ws, "c1")
    assert _turn_end_subscriber_count(bus) == 1
    reopened = _followup_streams(server)[0]
    assert not reopened.done
    bus.publish(_turn_end_event(turn_id="turn-live", session_id="sess-1"))
    assert reopened.done


@pytest.mark.asyncio
async def test_followup_turn_end_falls_back_to_session() -> None:
    """turn_id 对不上（注入瞬间快照换号）时按 session 收尾该会话的跟话流。"""
    server, _ws, bus = await _make_followup_server()
    stream = _followup_streams(server)[0]

    bus.publish(_turn_end_event(turn_id="turn-other", session_id="sess-1", reason="interrupted"))

    assert stream.done
    ends = [f for f in stream.buffer if f.get("type") == "turn_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "interrupted"


@pytest.mark.asyncio
async def test_followup_turn_end_spares_normal_stream_and_other_scopes() -> None:
    """收尾只碰跟话流：开局回合流（键=turn_id）与它端/子智能体回合都不动。"""
    from src.ui.turn_stream import TurnStream

    server, _ws, bus = await _make_followup_server()
    followup = _followup_streams(server)[0]
    normal = TurnStream("turn-live", "cli-attached", "root", server, channel_id="c1", session_id="sess-1")
    server._turns["turn-live"] = normal

    # 子智能体回合（origin_scope=subagent_loop）与主会话跟话流无关
    bus.publish(_turn_end_event(turn_id="turn-live", session_id="sess-1", origin_scope="subagent_loop"))
    assert not followup.done and not normal.done
    # 别的会话的回合不碰
    bus.publish(_turn_end_event(turn_id="t2", session_id="sess-other"))
    assert not followup.done and not normal.done
    # 无 turn_id / session_id 的事件直接跳过
    bus.publish(_turn_end_event(turn_id="", session_id=""))
    assert not followup.done and not normal.done

    # 本回合收尾：跟话流收尾，开局回合流仍归它自己的 finally
    bus.publish(_turn_end_event(turn_id="turn-live", session_id="sess-1"))
    assert followup.done
    assert not normal.done


@pytest.mark.asyncio
async def test_drain_continuation_returns_items() -> None:
    """drain_continuation → continuation_drained（items 含 text/image_blocks/source）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    items = [SimpleNamespace(text="a", image_blocks=None, source="cli-attached")]
    coara = MagicMock()
    coara.drain_continuation_inputs = MagicMock(return_value=items)
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)

    await server._handle_attach_message({"type": "drain_continuation"}, ws, "c1")

    frames = _sent_frames(ws)
    drained = [f for f in frames if f.get("type") == "continuation_drained"]
    assert drained and drained[0]["items"] == [{"text": "a", "image_blocks": None, "source": "cli-attached"}]


@pytest.mark.asyncio
async def test_cancel_continuation_pops_queue_tail() -> None:
    """cancel_continuation 撤回缓冲队列尾部跟话（内核无原语，直接尾删）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    coara = SimpleNamespace(
        _continuation_inputs=[SimpleNamespace(text="x"), SimpleNamespace(text="y")],
        _continuation_event=MagicMock(),
    )
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await _register_pin(server, ws)

    await server._handle_attach_message({"type": "cancel_continuation"}, ws, "c1")

    frames = _sent_frames(ws)
    cancelled = [f for f in frames if f.get("type") == "continuation_cancelled"]
    assert cancelled and cancelled[0]["cancelled"] is True
    assert len(coara._continuation_inputs) == 1

    # 空队列 → cancelled=False
    coara._continuation_inputs.clear()
    ws2 = _make_ws()
    await server._handle_attach_message({"type": "cancel_continuation"}, ws2, "c1")
    frames2 = _sent_frames(ws2)
    cancelled2 = [f for f in frames2 if f.get("type") == "continuation_cancelled"]
    assert cancelled2 and cancelled2[0]["cancelled"] is False


@pytest.mark.asyncio
async def test_pending_report_consumed_and_miss() -> None:
    """pending_report：命中 → consumed=True 带结构化 result；未命中 → consumed=False。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    server._bg_tasks = set()
    ws = _make_ws()
    await _register_pin(server, ws)

    import src.coara.commands.report as report_mod

    orig = report_mod.try_consume_pending_report_async
    report_mod.try_consume_pending_report_async = AsyncMock(
        return_value=SimpleNamespace(output="已提交", action=None, data={"ok": True}, exit_session=False)
    )
    try:
        await server._handle_attach_message({"type": "pending_report", "text": "崩溃详情"}, ws, "c1")
        if server._bg_tasks:
            await asyncio.gather(*server._bg_tasks, return_exceptions=True)
    finally:
        report_mod.try_consume_pending_report_async = orig

    frames = _sent_frames(ws)
    hit = [f for f in frames if f.get("type") == "pending_report_result"]
    assert hit and hit[0]["consumed"] is True
    assert hit[0]["result"]["output"] == "已提交"

    # 未命中
    server._bg_tasks = set()
    ws2 = _make_ws()
    report_mod.try_consume_pending_report_async = AsyncMock(return_value=None)
    try:
        await server._handle_attach_message({"type": "pending_report", "text": "普通消息"}, ws2, "c1")
        if server._bg_tasks:
            await asyncio.gather(*server._bg_tasks, return_exceptions=True)
    finally:
        report_mod.try_consume_pending_report_async = orig
    frames2 = _sent_frames(ws2)
    miss = [f for f in frames2 if f.get("type") == "pending_report_result"]
    assert miss and miss[0]["consumed"] is False


@pytest.mark.asyncio
async def test_chat_passes_image_blocks() -> None:
    """chat 带 image_blocks 时透传 process_message（多模态回合）。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    captured: dict = {}

    async def _agen(*args, **kwargs):
        captured["kwargs"] = kwargs
        yield "回复"
        return

    coara = MagicMock()
    coara.has_active_turn = MagicMock(return_value=False)
    coara.process_message = MagicMock(side_effect=_agen)
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()

    import src.ui.web_server as ws_mod

    class _FakeStream:
        def __init__(self, *a, **k):
            self.emitted: list[tuple[str, dict]] = []
            self.done = False

        def emit(self, kind, **payload):
            self.emitted.append((kind, payload))

        def emit_user_message(self, text, *, attachments=None, client_msg_id=None):
            kwargs = {"content": text, "attachments": list(attachments or [])}
            if client_msg_id:
                kwargs["client_msg_id"] = client_msg_id
            self.emit("user_message", **kwargs)

        def finish(self):
            self.done = True

    orig_stream = ws_mod.TurnStream
    orig_turn = ws_mod.turn
    ws_mod.TurnStream = _FakeStream

    class _FakeRemote:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    ws_mod.turn = _FakeRemote
    try:
        blocks = [{"type": "image", "data": "xyz"}]
        await server._handle_attach_chat({"type": "chat", "text": "看图", "image_blocks": blocks}, ws, "c1", "ws-a")
    finally:
        ws_mod.TurnStream = orig_stream
        ws_mod.turn = orig_turn

    assert captured["kwargs"].get("image_blocks") == blocks
    assert captured["kwargs"].get("source") == "cli-attached"


@pytest.mark.asyncio
async def test_chat_at_daily_routes_to_desk_keeps_pin() -> None:
    """@daily 投递到 daily 会话；pin 空间会话不被 process_message。"""
    server = _make_server(foreground_id="ws-other", entry=_entry())
    pin_calls: list[Any] = []
    desk_calls: list[Any] = []

    async def _pin_agen(*args, **kwargs):
        pin_calls.append(kwargs)
        if False:
            yield ""
        return

    async def _desk_agen(*args, **kwargs):
        desk_calls.append((args, kwargs))
        yield "日报好了"
        return

    pin_coara = MagicMock()
    pin_coara.has_active_turn = MagicMock(return_value=False)
    pin_coara.process_message = MagicMock(side_effect=_pin_agen)
    pin_coara.session_id = "sess-pin"
    server.root._sessions["ws-a"] = SimpleNamespace(coara=pin_coara)

    desk_coara = MagicMock()
    desk_coara.has_active_turn = MagicMock(return_value=False)
    desk_coara.process_message = MagicMock(side_effect=_desk_agen)
    desk_coara.session_id = "sess-daily"

    import src.ui.attach_ws as attach_mod

    class _FakeStream:
        def __init__(self, *a, **k):
            self.emitted: list[tuple[str, dict]] = []
            self.done = False
            self.desk = ""

        def emit(self, kind, **payload):
            self.emitted.append((kind, payload))

        def emit_user_message(self, text, *, attachments=None, client_msg_id=None):
            kwargs = {"content": text, "attachments": list(attachments or [])}
            if client_msg_id:
                kwargs["client_msg_id"] = client_msg_id
            self.emit("user_message", **kwargs)

        def finish(self):
            self.done = True

    class _FakeRemote:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    streams: list[_FakeStream] = []

    def _stream_factory(*a, **k):
        s = _FakeStream(*a, **k)
        streams.append(s)
        return s

    orig_stream = attach_mod.TurnStream
    orig_turn = attach_mod.turn
    attach_mod.TurnStream = _stream_factory  # type: ignore[misc,assignment]
    attach_mod.turn = _FakeRemote  # type: ignore[misc,assignment]

    async def _resolve(root, name, *, web_server=None):
        assert name == "daily"
        return desk_coara

    import src.coara.service_desk as desk_mod

    orig_resolve = desk_mod.resolve_service_desk_coara
    desk_mod.resolve_service_desk_coara = _resolve  # type: ignore[assignment]
    try:
        ws = _make_ws()
        await server._handle_attach_chat({"type": "chat", "text": "@daily 今天摘要"}, ws, "c1", "ws-a")
    finally:
        attach_mod.TurnStream = orig_stream
        attach_mod.turn = orig_turn
        desk_mod.resolve_service_desk_coara = orig_resolve

    assert pin_calls == []
    assert desk_calls, "daily coara should process_message"
    assert desk_calls[0][0][0] == "今天摘要"
    # 记录空间与 daily 合并：台签归一为展示名「记录」（@daily/@记录 同一会话）
    assert streams and streams[0].desk == "记录"


@pytest.mark.asyncio
async def test_chat_at_desk_empty_body_errors() -> None:
    server = _make_server(foreground_id="ws-other", entry=_entry())
    coara = MagicMock()
    coara.has_active_turn = MagicMock(return_value=False)
    server.root._sessions["ws-a"] = SimpleNamespace(coara=coara)
    ws = _make_ws()
    await server._handle_attach_chat({"type": "chat", "text": "@daily"}, ws, "c1", "ws-a")
    frames = _sent_frames(ws)
    errs = [f for f in frames if f.get("type") == "error"]
    assert errs and "后面写要说的话" in str(errs[0].get("message") or errs[0].get("error") or "")

    server = _make_server(foreground_id="ws-other", entry=_entry())
    ws = _make_ws()
    await _register_pin(server, ws)
    await server._handle_attach_message({"type": "ping"}, ws, "c1")
    frames = _sent_frames(ws)
    assert frames and frames[-1].get("type") == "pong"


# ----------------------------------------------------------------------
# 事件流：attach 连接按 pin 空间视图收完整 TraceEvent 帧
# ----------------------------------------------------------------------


def _make_trace_server(view_coara: SimpleNamespace) -> WebServer:
    """只够 _on_trace_event 跑的替身：无浏览器、一条 attach 连接 pin ws-a。"""
    from src.ui.attach_registry import AttachRegistry

    server = WebServer.__new__(WebServer)
    server.root = SimpleNamespace(
        _sessions={"ws-a": SimpleNamespace(coara=view_coara)},
        workspace_manager=None,
    )
    server.registry = SimpleNamespace(has_active=lambda: False)
    server.attach_registry = AttachRegistry()
    server._module_roots = {}
    server._trace_batch = []
    import asyncio as _asyncio

    server._trace_batch_lock = _asyncio.Lock()
    server._ensure_trace_flush_task = lambda: None
    return server


def _trace_event(event_type: str, **payload) -> Any:
    from src.core.events import TraceEvent

    return TraceEvent(coara_id="c1", coara_name="考拉", event_type=event_type, message="m", payload=payload)


def _attach_frame(server: WebServer) -> dict:
    return server._trace_batch[-1]["attach"]["conn-1"]


class _KindRecordingStream:
    """记录 (帧型, 载荷) 的 TurnStream 替身（不微批、不广播）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []
        self.done = False
        self.desk = ""
        self.workspace_id = ""

    def emit(self, kind: str, **payload: Any) -> None:
        self.emitted.append((kind, payload))

    def emit_user_message(
        self, text: str, *, attachments: list | None = None, client_msg_id: str | None = None
    ) -> None:
        kwargs: dict[str, Any] = {"content": text, "attachments": list(attachments or [])}
        if client_msg_id:
            kwargs["client_msg_id"] = client_msg_id
        self.emit("user_message", **kwargs)

    def finish(self) -> None:
        self.done = True


class _NullTurn:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _NullTurn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_attach_turn_sender_never_flattens_subagent_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """子智能体过程帧不得并进 chunk 帧型。

    并了就是冒充主会话正文：CLI 按前台回合正文打进滚动区，与
    ``[<类型>子智能体]`` 结果回显行重复（同一份报告显示两遍的根因）。
    """
    from src.coara.end_registry import EndRegistry

    server = _make_server(foreground_id="ws-other", entry=_entry())
    registry = EndRegistry()
    server.root.end_registry = registry

    class _Coara:
        session_id = "sess-1"

        def has_active_turn(self) -> bool:
            return False

        async def process_message(self, *args: Any, **kwargs: Any):
            sender = registry._senders[("cli-attached", "sess-1")]
            sender({"kind": "chunk", "text": "主会话正文"})
            sender({"kind": "subagent_chunk", "text": "子智能体过程旁白", "tool_call_id": "c1", "coara_id": "sa-1"})
            sender({"kind": "subagent_result", "text": "子智能体最终报告", "tool_call_id": "c1", "coara_id": "sa-1"})
            sender({"kind": "tool", "text": "✓ shell"})
            yield "主会话正文"

    server.root._sessions["ws-a"] = SimpleNamespace(coara=_Coara())

    import src.ui.attach_ws as attach_mod

    streams: list[_KindRecordingStream] = []

    def _stream_factory(*args: Any, **kwargs: Any) -> _KindRecordingStream:
        stream = _KindRecordingStream()
        streams.append(stream)
        return stream

    monkeypatch.setattr(attach_mod, "TurnStream", _stream_factory)
    monkeypatch.setattr(attach_mod, "turn", _NullTurn)

    await server._handle_attach_chat({"type": "chat", "text": "干活"}, _make_ws(), "c1", "ws-a")

    assert streams, "回合应建流"
    emitted = streams[0].emitted
    by_kind = dict(emitted)
    # 关键：子智能体文本一条都不许进 chunk 帧型（CLI 会把 chunk 当前台正文）
    assert [p["text"] for k, p in emitted if k == "chunk"] == ["主会话正文"]
    assert by_kind["subagent_chunk"]["text"] == "子智能体过程旁白"
    assert by_kind["subagent_chunk"]["agent_kind"] == "subagent"
    assert by_kind["subagent_result"]["text"] == "子智能体最终报告"
    assert by_kind["subagent_result"]["agent_kind"] == "subagent"
    # 工具行帧照旧不投（CLI 工具行走自己的 scrollback 链路）
    assert "tool" not in by_kind


def test_attach_diff_frame_carries_agent_attribution() -> None:
    """diff 帧带归属字段（纯新增）：子智能体改动仍渲染，端侧可忽略该字段。"""
    import src.ui.attach_ws as attach_mod

    kind, payload = attach_mod._attach_output_frame(
        {"kind": "diff", "display_blocks": [{"kind": "diff"}], "tool_name": "edit", "parent_tool_call_id": "call-1"}
    )
    assert kind == "diff"
    assert payload["agent_kind"] == "subagent"
    assert payload["parent_tool_call_id"] == "call-1"

    kind, payload = attach_mod._attach_output_frame({"kind": "diff", "display_blocks": [], "tool_name": "edit"})
    assert kind == "diff" and payload["agent_kind"] == "main"


@pytest.mark.asyncio
async def test_attach_trace_full_topic_set_reaches_connection() -> None:
    """必传 topic 全集（含浏览器不收的 llm_switched 等）都进 attach 帧。"""
    from src.ui.trace_broadcast import _END_SCOPED_TRACE_TYPES

    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    # conversation_message 是 trace/录像带素材，不推 attach（正文单一事实源
    # 是 chunk 流 + turn_end；推了会在断连重连窗口双显）。
    for topic in sorted(server._ATTACH_TRACE_TOPICS - {"conversation_message"}):
        server._trace_batch.clear()
        payload: dict[str, Any] = {"session_id": "sess-1", "workspace_dir": "D:/ws/a"}
        # 端作用域 topic 必须带发起端；后台完成用 origin_source（与生产 emit 一致）
        if topic in _END_SCOPED_TRACE_TYPES:
            if topic.startswith("background_"):
                payload["origin_source"] = "cli-attached"
            else:
                payload["source"] = "cli-attached"
        server._on_trace_event(_trace_event(topic, **payload))
        assert server._trace_batch, f"{topic} 应入批"
        frame = server._trace_batch[-1]["attach"].get("conn-1")
        assert frame is not None, f"{topic} 应推 attach 连接"
        assert frame["type"] == topic
        assert frame["coara_id"] == "c1"


@pytest.mark.asyncio
async def test_attach_trace_filters_other_workspace_events() -> None:
    """显式归属其它空间的事件不推本连接（视图过滤按 pin 空间）。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    server._on_trace_event(
        _trace_event(
            "chat_chunk",
            session_id="sess-other",
            workspace_dir="D:/ws/b",
            source="cli-attached",
        )
    )
    assert not server._trace_batch or "conn-1" not in server._trace_batch[-1]["attach"]


@pytest.mark.asyncio
async def test_attach_trace_passes_local_subagent_tool_events() -> None:
    """本空间子智能体（origin_scope=subagent_loop，sa-* sid）的工具事件推 attach——
    发起端要看到子智能体的工具调用，不能按「其它会话」过滤掉。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    server._on_trace_event(
        _trace_event(
            "tool_complete",
            session_id="sa-flow-abc",
            workspace_dir="D:/ws/a",
            origin_scope="subagent_loop",
            source="cli-attached",
            tool="edit",
            display_blocks=[{"kind": "diff"}],
        )
    )
    frame = server._trace_batch[-1]["attach"].get("conn-1")
    assert frame is not None, "本空间子智能体 tool_complete 应推 attach"
    assert frame["type"] == "tool_complete"


@pytest.mark.asyncio
async def test_attach_trace_filters_other_workspace_subagent() -> None:
    """其它空间的子智能体工具事件仍不推本连接。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    server._on_trace_event(
        _trace_event(
            "tool_complete",
            session_id="sa-flow-abc",
            workspace_dir="D:/ws/b",
            origin_scope="subagent_loop",
            source="cli-attached",
        )
    )
    assert not server._trace_batch or "conn-1" not in server._trace_batch[-1]["attach"]


@pytest.mark.asyncio
async def test_attach_trace_loose_passes_sidless_lifecycle_events() -> None:
    """父代发的无 sid 子智能体生命周期事件（subagent_start）宽松放行。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    server._on_trace_event(
        _trace_event(
            "subagent_start",
            subagent_type="coaras",
            subagent_id="sa-1",
            description="干活",
            source="cli-attached",
        )
    )
    frame = _attach_frame(server)
    assert frame["type"] == "subagent_start"
    assert frame["subagent_id"] == "sa-1"
    # 无归属线索 → 标 detached（客户端可徽标化）
    assert frame.get("detached") is True


@pytest.mark.asyncio
async def test_attach_trace_field_fidelity() -> None:
    """事件帧保真：message/payload 关键字段原样带（text/content/tool_name 等）。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    server._on_trace_event(
        _trace_event(
            "tool_start",
            session_id="sess-1",
            workspace_dir="D:/ws/a",
            tool_name="shell",
            tool_call_id="call-9",
            arguments={"command": "ls"},
            turn_id="t-7",
            source="cli-attached",
        )
    )
    frame = _attach_frame(server)
    assert frame["type"] == "tool_start"
    assert frame["message"] == "m"
    assert frame["turn_id"] == "t-7"
    assert frame["source"] == "cli-attached"
    # 工具字段归一（tool_name→tool, tool_call_id→call_id, arguments→args）
    assert frame.get("tool") == "shell"
    assert frame.get("call_id") == "call-9"
    assert frame.get("args") == {"command": "ls"}


@pytest.mark.asyncio
async def test_attach_trace_flush_sends_per_connection_batch() -> None:
    """flush：每连接定向一条 trace_batch（帧按其视图序列化）；无浏览器不报错。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws = _make_ws()
    await server.attach_registry.register(ws, "conn-1", "ws-a")

    server._on_trace_event(
        _trace_event(
            "user_message",
            session_id="sess-1",
            workspace_dir="D:/ws/a",
            text="hi",
            source="cli-attached",
        )
    )
    server._on_trace_event(
        _trace_event(
            "chat_chunk",
            session_id="sess-1",
            workspace_dir="D:/ws/a",
            text="你",
            source="cli-attached",
        )
    )
    sent = await server._flush_trace_batch()
    assert sent is True
    frames = _sent_frames(ws)
    batch = [f for f in frames if f.get("type") == "trace_batch"]
    assert batch, "attach 连接应收 trace_batch"
    events = batch[0]["events"]
    assert [e["type"] for e in events] == ["user_message", "chat_chunk"]
    assert server._trace_batch == []


@pytest.mark.asyncio
async def test_attach_trace_channel_id_isolates_dual_cli() -> None:
    """同空间双 attach：带 channel_id 的端作用域事件只推发起连接。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws_a = _make_ws()
    ws_b = _make_ws()
    await server.attach_registry.register(ws_a, "conn-a", "ws-a")
    await server.attach_registry.register(ws_b, "conn-b", "ws-a")

    server._on_trace_event(
        _trace_event(
            "thinking_progress",
            session_id="sess-1",
            workspace_dir="D:/ws/a",
            source="cli-attached",
            channel_id="conn-a",
            text="…",
        )
    )
    assert server._trace_batch, "应入批"
    attach_map = server._trace_batch[-1]["attach"]
    assert "conn-a" in attach_map
    assert "conn-b" not in attach_map


@pytest.mark.asyncio
async def test_attach_trace_without_channel_still_reaches_all_same_workspace() -> None:
    """无 channel_id（旧事件 / chrome）仍推同空间所有 attach。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws_a = _make_ws()
    ws_b = _make_ws()
    await server.attach_registry.register(ws_a, "conn-a", "ws-a")
    await server.attach_registry.register(ws_b, "conn-b", "ws-a")

    server._on_trace_event(
        _trace_event(
            "thinking_progress",
            session_id="sess-1",
            workspace_dir="D:/ws/a",
            source="cli-attached",
        )
    )
    attach_map = server._trace_batch[-1]["attach"]
    assert "conn-a" in attach_map and "conn-b" in attach_map


@pytest.mark.asyncio
async def test_attach_llm_switched_ignores_channel_id() -> None:
    """llm_switched 是同空间 chrome：即使带 channel_id 也推所有共看连接。"""
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    server = _make_trace_server(view)
    ws_a = _make_ws()
    ws_b = _make_ws()
    await server.attach_registry.register(ws_a, "conn-a", "ws-a")
    await server.attach_registry.register(ws_b, "conn-b", "ws-a")

    server._on_trace_event(
        _trace_event(
            "llm_switched",
            session_id="sess-1",
            workspace_dir="D:/ws/a",
            workspace_id="ws-a",
            channel_id="conn-a",
            model_name="m",
        )
    )
    attach_map = server._trace_batch[-1]["attach"]
    assert "conn-a" in attach_map and "conn-b" in attach_map
