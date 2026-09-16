"""Web UI runtime state includes foreground workspace name after switch."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.types import ContinuationInput
from src.ui.turn_stream import TurnStream
from src.ui.view_recorder import shared_view_store
from src.ui.web_server import WebServer
from src.ui.web_views import WebViewStore, resolve_web_view_path
from src.workspace.manager import WorkspaceManager


def _permit_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    # 无 provider 早失败检查在隔离测试环境会拦掉回合（无 key），桩成总有可用 provider
    monkeypatch.setattr("src.llm.model_catalog._enabled_provider_names", lambda _mgr: ["fake"])


@pytest.mark.asyncio
async def test_current_runtime_includes_workspace_name(tmp_path: Path) -> None:
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()

    manager = WorkspaceManager(ws_a, coara_home=coara_home)
    await manager.initialize()
    # initialize() may already register cwd as folder name; re-ensure with aliases.
    entry_a = manager.registry.ensure_workspace(ws_a, name="alpha")
    entry_b = manager.registry.ensure_workspace(ws_b, name="beta")
    # Force display names even if path-derived ids already existed.
    entry_a.name = "alpha"
    entry_b.name = "beta"
    manager.registry.save()
    assert manager.switch(entry_b.id)

    fg = SimpleNamespace(
        session_id="sid-b",
        status=SimpleNamespace(value="idle"),
        provider_name="p",
        model_name="m",
        has_active_turn=lambda: False,
        workspace_dir=entry_b.resolved_path(),
    )
    root = SimpleNamespace(
        foreground_coara=fg,
        workspace_manager=manager,
    )

    server = WebServer.__new__(WebServer)
    server.root = root
    server._last_runtime_snapshot = None

    runtime = WebServer._current_runtime(server)
    assert runtime["workspace_name"] == "beta"
    assert runtime["session_id"] == "sid-b"
    assert Path(runtime["workspace_dir"]) == entry_b.resolved_path()
    assert runtime["context_used_tokens"] == 0
    assert runtime["context_window_tokens"] == 0
    assert runtime["context_cache_hit_ratio"] is None

    assert manager.switch(entry_a.id)
    fg.workspace_dir = entry_a.resolved_path()
    fg.session_id = "sid-a"
    runtime2 = WebServer._current_runtime(server)
    assert runtime2["workspace_name"] == "alpha"
    assert runtime2["session_id"] == "sid-a"


def test_current_runtime_includes_provider_context_usage() -> None:
    from src.context.window import LlmUsageSnapshot

    snap = LlmUsageSnapshot()
    snap.record_turn(
        usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 400,
        },
        history_len=10,
        system_len=1,
        tool_count=0,
    )
    provider = SimpleNamespace(get_context_window=lambda _model: 200_000)
    fg = SimpleNamespace(
        session_id="sid",
        status=SimpleNamespace(value="idle"),
        provider_name="p",
        model_name="m",
        provider=provider,
        has_active_turn=lambda: False,
        workspace_dir=Path("/tmp/ws"),
        _llm_usage_snapshot=snap,
    )
    server = WebServer.__new__(WebServer)
    server.root = SimpleNamespace(foreground_coara=fg, workspace_manager=None)
    server._last_runtime_snapshot = None

    runtime = WebServer._current_runtime(server)
    # prompt (1000+400) + output 200
    assert runtime["context_used_tokens"] == 1600
    assert runtime["context_window_tokens"] == 200_000
    assert runtime["context_cache_hit_ratio"] == pytest.approx(400 / 1400)


def test_trace_event_tags_detached_workspace_events(tmp_path: Path) -> None:
    """Mid-turn switch: departing session's events are still broadcast,
    tagged with detached + workspace_name instead of being dropped."""
    from src.core.events import TraceEvent

    ws_a = tmp_path / "shop"
    ws_b = tmp_path / "main"
    ws_a.mkdir()
    ws_b.mkdir()

    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(session_id="sb", workspace_dir=str(ws_b)),
        workspace_manager=None,
    )
    server = WebServer.__new__(WebServer)
    server.root = root
    server.registry = SimpleNamespace(has_active=lambda: True)
    server.attach_registry = SimpleNamespace(has_connections=lambda: False)
    server._module_roots = {}
    server._trace_batch = []
    server._ensure_trace_flush_task = lambda: None

    def chunk(session_id: str, ws: Path) -> TraceEvent:
        return TraceEvent(
            coara_id="c1",
            coara_name="考拉",
            event_type="chat_chunk",
            message="x",
            payload={
                "text": "回复片段",
                "source": "web",
                "turn_id": "t1",
                "session_id": session_id,
                "workspace_dir": str(ws),
            },
        )

    # Foreground event: no detached marker.
    server._on_trace_event(chunk("sb", ws_b))
    browser_frame = server._trace_batch[-1]["browser"]
    assert browser_frame.get("detached") is None
    assert "workspace_name" not in browser_frame

    # Departing workspace A still streaming after switch: tagged, not dropped.
    server._on_trace_event(chunk("sa", ws_a))
    last = server._trace_batch[-1]["browser"]
    assert last["detached"] is True
    assert last["workspace_name"] == "shop"


@pytest.mark.asyncio
async def test_handle_chat_leftover_stays_on_origin_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回合中切空间后 leftover continuation 必须在原 session 上跑 不落到新前台"""
    _permit_providers(monkeypatch)
    import contextlib
    from unittest.mock import AsyncMock

    class _FakeCoara:
        def __init__(self, session_id: str, workspace_dir: Path, chunks: list[str]) -> None:
            self.session_id = session_id
            self.workspace_dir = workspace_dir
            self._chunks = chunks
            self.calls = 0
            self._continuation_inputs: list[ContinuationInput] = []
            self.on_first_chunk = None

        def has_active_turn(self) -> bool:
            return False

        def submit_continuation_input(
            self,
            text: str,
            image_blocks: list[dict] | None = None,
            *,
            source: str = "",
            client_msg_id: str = "",
        ) -> None:
            self._continuation_inputs.append(ContinuationInput(text=text, image_blocks=image_blocks, source=source))

        async def process_message(self, *_args, **_kwargs):
            self.calls += 1
            for i, chunk in enumerate(self._chunks):
                if i == 0 and self.on_first_chunk is not None:
                    self.on_first_chunk()
                yield chunk

        def drain_continuation_inputs(self) -> list[ContinuationInput]:
            items = list(self._continuation_inputs)
            self._continuation_inputs.clear()
            return items

    ws_a = tmp_path / "alpha"
    ws_b = tmp_path / "beta"
    ws_a.mkdir()
    ws_b.mkdir()
    a = _FakeCoara("sa", ws_a, ["c1", "c2"])
    b = _FakeCoara("sb", ws_b, ["x"])
    a._continuation_inputs = [ContinuationInput(text="追问")]  # queued while A was foreground

    class _Root:
        def __init__(self) -> None:
            self._fg = a
            self._foreground_session_id = "ws-a"

        @property
        def foreground_coara(self):
            return self._fg

        def switch(self) -> None:
            self._fg = b
            self._foreground_session_id = "ws-b"

    root = _Root()
    a.on_first_chunk = root.switch  # first chunk arrives, then focus moves to B

    @contextlib.asynccontextmanager
    async def _dummy_remote_turn(*_args, **_kwargs):
        yield

    import src.ui.web_server as web_mod

    orig_turn = web_mod.turn
    web_mod.turn = _dummy_remote_turn
    try:
        server = WebServer.__new__(WebServer)
        server.root = root
        server.interaction_channel = SimpleNamespace()
        server._turn_tails = {}
        server.registry = SimpleNamespace(
            has_active=lambda: False,
            send_to_active=AsyncMock(return_value=False),
            send_to_active_nowait=lambda _msg: None,
        )

        ws = SimpleNamespace(send_str=AsyncMock(), closed=False)
        await server._handle_chat({"text": "hi"}, ws, "conn-1")
        # Drain tasks for detached turns run in the background.
        import asyncio

        await asyncio.sleep(0.1)

        assert b.calls == 0
        # Initial turn + drained leftover continuation, both on A.
        assert a.calls == 2
    finally:
        web_mod.turn = orig_turn


@pytest.mark.asyncio
async def test_interrupt_targets_frame_workspace_not_current_view(tmp_path: Path) -> None:
    """P0-7: interrupt with workspace_dir stops that space's turn, not the post-switch view."""
    from unittest.mock import MagicMock

    ws_a = tmp_path / "alpha"
    ws_b = tmp_path / "beta"
    ws_a.mkdir()
    ws_b.mkdir()

    coara_a = SimpleNamespace(interrupt_current_turn=MagicMock(return_value=True))
    coara_b = SimpleNamespace(interrupt_current_turn=MagicMock(return_value=True))

    class _Root:
        def resolve_workspace_coara(self, wid: str):
            # workspace_id_for of alpha/beta paths — match by suffix in test via monkeypatch below
            return {"id-a": coara_a, "id-b": coara_b}.get(wid)

    server = WebServer.__new__(WebServer)
    server.root = _Root()
    server._module_roots = {}
    server._view_coara = lambda: coara_b  # live view already on B

    def _fake_id(path):
        p = str(path)
        if "alpha" in p:
            return "id-a"
        if "beta" in p:
            return "id-b"
        return "other"

    import src.core.coara_home as home_mod

    real = home_mod.workspace_id_for
    home_mod.workspace_id_for = _fake_id  # type: ignore[assignment]
    try:
        await server._handle_ws_message(
            {"type": "interrupt", "workspace_dir": str(ws_a)},
            MagicMock(),
            "conn-1",
        )
    finally:
        home_mod.workspace_id_for = real  # type: ignore[assignment]

    coara_a.interrupt_current_turn.assert_called_once_with("user_stop", interrupt_source="stop_command")
    coara_b.interrupt_current_turn.assert_not_called()


@pytest.mark.asyncio
async def test_flow_root_global_across_workspace_switch(tmp_path: Path) -> None:
    """FlowRoot 全局化：切换前台工作空间不重建实例，workspace_dir 跟随前台。

    工作流页面是全局工作台：图、历史与固化资产不随空间切换销毁；新
    spawn 的节点默认跑在当前前台空间。
    """
    from tests.helpers import make_test_coara

    ws_a = tmp_path / "flow-a"
    ws_b = tmp_path / "flow-b"
    ws_a.mkdir()
    ws_b.mkdir()

    fg = make_test_coara(tmp_path)
    fg.workspace_dir = str(ws_a)
    fake_root = SimpleNamespace(
        foreground_coara=fg,
        event_bus=SimpleNamespace(publish=lambda ev: None),
        workspace_manager=None,
    )

    server = WebServer.__new__(WebServer)
    server.root = fake_root
    server._module_roots = {}
    server._module_roots_lock = asyncio.Lock()

    first = await server._get_flow_root()
    assert isinstance(first.workspace_dir, Path)
    assert first.workspace_dir == ws_a.resolve()

    # 切换前台空间到 B：同一实例保留（图不销毁），workspace_dir 跟随
    fg.workspace_dir = str(ws_b)
    second = await server._get_flow_root()
    assert second is first
    assert isinstance(second.workspace_dir, Path)
    assert second.workspace_dir == ws_b.resolve()
    assert second.is_flow_subject()
    assert second._session_log is not None
    assert second._session_log._agent_kind == "flow"
    # 回归：同步后仍可 spawn（旧 bug 把 workspace_dir 写成 str，load_skills 里 str/str 炸）
    msg = await second.flow_coordinator.spawn_node(
        second,
        flow="probe",
        node_id="n1",
        task="ping",
    )
    assert "已注册" in msg
    st = second.flow_coordinator.get_flow("probe").states["n1"]
    assert st.coara is not None


@pytest.mark.asyncio
async def test_web_followup_injected_into_active_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """主会话活跃回合时 Web 第二条消息与手机端同待遇：包装远端标记注入
    continuation 队列 + 登记回投镜像（回复流回本 Web 客户端），不新开回合；
    flow 页面保持注入语义。"""
    _permit_providers(monkeypatch)
    from unittest.mock import AsyncMock, patch

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    class _FakeCoara:
        def __init__(self) -> None:
            self.submitted: list[str] = []
            self.mirror_ctx: tuple | None = None
            self.session_id = "sb"
            self.workspace_dir = str(ws_dir)

        def has_active_turn(self) -> bool:
            return True

        def set_deferred_remote_ctx(
            self, channel_id, send_text, interaction_channel, *, source: str = "", actor: str = ""
        ) -> None:
            self.mirror_ctx = (channel_id, send_text, interaction_channel)

        def submit_continuation_input(
            self,
            text: str,
            image_blocks: list[dict] | None = None,
            *,
            source: str = "",
            client_msg_id: str = "",
        ) -> None:
            self.submitted.append(text)

        def drain_continuation_inputs(self) -> list[ContinuationInput]:
            return []

    fake = _FakeCoara()
    root = SimpleNamespace(
        foreground_coara=fake,
        _foreground_session_id="sb",
        workspace_manager=None,
    )
    server = WebServer.__new__(WebServer)
    server.root = root
    server.interaction_channel = SimpleNamespace()
    server._turn_tails = {}
    server._module_roots = {}
    server._module_roots_lock = asyncio.Lock()
    server.registry = SimpleNamespace(
        has_active=lambda: False,
        send_to_active=AsyncMock(return_value=False),
        send_to_active_nowait=lambda _msg: None,
    )

    ws = SimpleNamespace(send_str=AsyncMock())
    with (
        patch.object(server, "_stream_chat_turn", new=AsyncMock()) as mock_stream,
        patch("src.coara.commands.report.try_consume_pending_report_async", new=AsyncMock(return_value=None)),
    ):
        await server._handle_chat({"text": "b"}, ws, "conn-1", subject="root")

    assert len(fake.submitted) == 1
    assert fake.submitted[0] == "b"  # 裸文本提交，来源标签由内核按 source=web 现包
    assert fake.mirror_ctx is not None and fake.mirror_ctx[0] == "conn-1"  # 回投镜像已登记
    mirror_send = fake.mirror_ctx[1]
    await mirror_send("conn-1", "mirror text")
    server.registry.send_to_active.assert_awaited()
    payload = server.registry.send_to_active.await_args.args[0]
    assert payload["type"] == "chunk"
    assert payload["source"] == "web"
    mock_stream.assert_not_awaited()  # 不新开回合

    class _FakeFlowRoot:
        def __init__(self) -> None:
            self.submitted: list[str] = []
            self.session_id = "flow-session"
            self.workspace_dir = str(ws_dir)

        def has_active_turn(self) -> bool:
            return True

        def submit_continuation_input(
            self,
            text: str,
            image_blocks: list[dict] | None = None,
            *,
            source: str = "",
            client_msg_id: str = "",
        ) -> None:
            self.submitted.append(text)

        def drain_continuation_inputs(self) -> list[ContinuationInput]:
            return []

    server._module_roots["flow"] = _FakeFlowRoot()
    with patch("src.coara.commands.report.try_consume_pending_report_async", new=AsyncMock(return_value=None)):
        await server._handle_chat({"text": "b"}, ws, "conn-1", subject="flow")
    assert server._module_roots["flow"].submitted == ["b"]  # flow 保持注入（原文 无远端标记）
    mock_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_followup_reregisters_end_registry_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """它端回合 web 跟话：无活跃 web TurnStream 时须覆盖 stale EndRegistry sender。"""
    _permit_providers(monkeypatch)
    from unittest.mock import AsyncMock, MagicMock, patch

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    class _Turn:
        turn_id = "t-foreign"

    class _FakeCoara:
        session_id = "sb"
        workspace_dir = str(ws_dir)
        _active_turn = _Turn()

        def has_active_turn(self) -> bool:
            return True

        def set_deferred_remote_ctx(
            self, channel_id, send_text, interaction_channel, *, source: str = "", actor: str = ""
        ) -> None:
            pass

        def submit_continuation_input(
            self, text: str, image_blocks=None, *, source: str = "", client_msg_id: str = ""
        ) -> None:
            pass

        def drain_continuation_inputs(self) -> list[ContinuationInput]:
            return []

    stale_sender = MagicMock()
    registry = MagicMock()
    registry.sender_for.return_value = stale_sender
    registry.register = MagicMock()
    registry.unregister = MagicMock()

    fake = _FakeCoara()
    root = SimpleNamespace(
        foreground_coara=fake,
        end_registry=registry,
        _foreground_session_id="sb",
        web_view_workspace_id=None,
    )
    server = WebServer.__new__(WebServer)
    server.root = root
    server.interaction_channel = SimpleNamespace()
    server._turn_tails = {}
    server._module_roots = {}
    server._module_roots_lock = asyncio.Lock()
    server._web_followup_view_turns = set()
    server.workspace_dir = ws_dir
    server.coara_home = None
    server._view_store = None
    server.registry = SimpleNamespace(
        has_active=lambda: False,
        send_to_active=AsyncMock(return_value=False),
        send_to_active_nowait=lambda _msg: None,
    )
    server._view_coara = lambda: fake  # type: ignore[method-assign]

    ws = SimpleNamespace(send_str=AsyncMock())
    with patch("src.coara.commands.report.try_consume_pending_report_async", new=AsyncMock(return_value=None)):
        await server._handle_chat({"text": "跟话2"}, ws, "conn-2", subject="root")

    # 它端回合跟话：无活跃 web TurnStream 时须新建 followup 流并登记 EndRegistry，
    # 覆盖 stale sender——禁止只注销不注册（段切到 web 后 chunk 会丢或回落错端）。
    registry.unregister.assert_called_once_with("web", stale_sender, "sb")
    registry.register.assert_called_once()
    assert registry.register.call_args.args[0] == "web"
    assert registry.register.call_args.args[2] == "sb"
    assert any(
        str(getattr(getattr(s, "route", None), "channel_id", "") or "") == "followup-web"
        for s in server._turns.values()
    )


@pytest.mark.asyncio
async def test_web_followup_registers_channel_during_awakened_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台唤醒回合（awakened TurnStream）进行中 web 跟话：必须注册 web 通道。

    事故场景（2026-09-07）：唤醒回合的 TurnStream 标 source="web" 但正文经
    background 通道送入，从未注册 ("web", session) 通道。旧闸门把这条流误判为
    「web 通道已在跑」跳过注册 → 段切到 web 后 chunk 查无通道静默丢弃，
    页面 spinner 停、正文不再显示。修复后 awakened-web 流不算数，照常注册。
    """
    _permit_providers(monkeypatch)
    from unittest.mock import AsyncMock, MagicMock, patch

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    class _Turn:
        turn_id = "t-awakened"

    class _FakeCoara:
        session_id = "sb"
        workspace_dir = str(ws_dir)
        _active_turn = _Turn()

        def has_active_turn(self) -> bool:
            return True

        def set_deferred_remote_ctx(
            self, channel_id, send_text, interaction_channel, *, source: str = "", actor: str = ""
        ) -> None:
            pass

        def submit_continuation_input(
            self, text: str, image_blocks=None, *, source: str = "", client_msg_id: str = ""
        ) -> None:
            pass

        def drain_continuation_inputs(self) -> list[ContinuationInput]:
            return []

    # 唤醒回合的流：source=web、channel_id=awakened-web、仍在跑——但它不持有
    # ("web", session) 通道（background sender 已在注册表里）
    awakened_stream = SimpleNamespace(
        session_id="sb",
        source="web",
        route=SimpleNamespace(channel_id="awakened-web"),
        done=False,
    )
    background_sender = MagicMock()
    registry = MagicMock()
    registry.sender_for.return_value = background_sender
    registry.register = MagicMock()
    registry.unregister = MagicMock()

    fake = _FakeCoara()
    root = SimpleNamespace(
        foreground_coara=fake,
        end_registry=registry,
        _foreground_session_id="sb",
        web_view_workspace_id=None,
    )
    server = WebServer.__new__(WebServer)
    server.root = root
    server.interaction_channel = SimpleNamespace()
    server._turn_tails = {}
    server._module_roots = {}
    server._module_roots_lock = asyncio.Lock()
    server._web_followup_view_turns = set()
    server.workspace_dir = ws_dir
    server.coara_home = None
    server._view_store = None
    server.registry = SimpleNamespace(
        has_active=lambda: False,
        send_to_active=AsyncMock(return_value=False),
        send_to_active_nowait=lambda _msg: None,
    )
    server._view_coara = lambda: fake  # type: ignore[method-assign]
    server._turns["t-awakened"] = awakened_stream

    ws = SimpleNamespace(send_str=AsyncMock())
    with patch("src.coara.commands.report.try_consume_pending_report_async", new=AsyncMock(return_value=None)):
        await server._handle_chat({"text": "跟话"}, ws, "conn-2", subject="root")

    # 唤醒流不算「web 通道已在跑」：须新建 followup TurnStream 并登记 ("web", session)，
    # 段切到 web 后 chunk 走 followup 流（buffer+落盘+WS），不得静默丢弃。
    registry.unregister.assert_called_once_with("web", background_sender, "sb")
    registry.register.assert_called_once()
    assert registry.register.call_args.args[0] == "web"
    assert registry.register.call_args.args[2] == "sb"
    assert any(
        str(getattr(getattr(s, "route", None), "channel_id", "") or "") == "followup-web"
        for s in server._turns.values()
    )


@pytest.mark.asyncio
async def test_web_turn_survives_workspace_switch_chunk_keeps_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：web 回合进行中切空间，sender 不得被注销——切走期间 chunk 继续
    路由进本回合 TurnStream（buffer + 视图落盘），切回后实时恢复显示。

    事故场景（2026-09-08 晚，session=340c9008）：旧实现切走时主迭代器经
    iter_while_foreground detach 立即返回，_stream_chat_turn 的 finally 无条件
    注销 ("web", session) sender；回合 async generator 由 drain 后台继续跑，
    后续 chunk 在 base._route_chunk_to_current_end 查无通道 → 持续打
    "route chunk: no sender for end 'web'" WARNING 且正文纯丢弃（连视图存储
    都不落，刷新也救不回），切回无任何机制重注册 → 该回合剩余正文永久丢失。
    """
    _permit_providers(monkeypatch)
    import contextlib
    from unittest.mock import AsyncMock

    from src.coara.end_registry import EndRegistry
    from src.llm.provider import LLMProvider, LLMResponse, StreamChunk
    from tests.helpers import make_test_coara

    ws_a = tmp_path / "alpha"
    ws_a.mkdir()

    release_tail = asyncio.Event()

    class _TailGatedProvider(LLMProvider):
        """前两个 chunk 即时产出；随后静默直到放行——模拟 LLM 长静默期。

        真实回合里静默期是 detach 窗口；放行后尾部 chunk 到达即推进路由，
        微批 flush（16ms call_later）在 drain 任务内完成。
        complete 聚合流式输出后返回（llm_service 无 hook 时走 complete），
        chunk 由 run_turn_loop 内 _route_chunk_to_current_end 按段路由。
        """

        def __init__(self) -> None:
            super().__init__(name="fake", api_key="test", default_model="fake-model")

        async def complete(self, *args, **kwargs) -> LLMResponse:
            text_parts: list[str] = []
            async for chunk in self.stream_complete(*args, **kwargs):
                if chunk.delta_content:
                    text_parts.append(chunk.delta_content)
            return LLMResponse(content="".join(text_parts), tool_calls=[])

        async def abort(self) -> None:
            return None

        async def close(self) -> None:
            return None

        def get_context_window(self, model: str | None = None) -> int:
            return 200_000

        async def stream_complete(self, *args, **kwargs):
            yield StreamChunk(delta_content="chunk-1 ")
            await asyncio.sleep(0)
            yield StreamChunk(delta_content="chunk-2 ")
            # 静默期：detach 轮询（0.2s）在此期间触发
            await release_tail.wait()
            yield StreamChunk(delta_content="tail-after-switch-back")
            # 给微批 flush（call_later 16ms）一个完成的调度节拍再结束
            await asyncio.sleep(0.05)

    fake = make_test_coara(ws_a, provider=_TailGatedProvider())

    class _Root:
        def __init__(self) -> None:
            self._foreground_session_id = "ws-a"
            self._web_view_workspace_id = "ws-a"
            self.end_registry = EndRegistry()

        @property
        def foreground_coara(self):
            return fake

    root = _Root()
    fake._root_ref = root

    @contextlib.asynccontextmanager
    async def _dummy_remote_turn(*_args, **_kwargs):
        yield

    import src.ui.web_server as web_mod

    orig_turn = web_mod.turn
    web_mod.turn = _dummy_remote_turn
    try:
        server = WebServer.__new__(WebServer)
        server.root = root
        server.interaction_channel = SimpleNamespace()
        server._turn_tails = {}
        server.registry = SimpleNamespace(
            has_active=lambda: True,
            send_to_active=AsyncMock(return_value=True),
            send_to_active_nowait=lambda msg: None,
        )
        server._view_store_persist = None

        ws = SimpleNamespace(send_str=AsyncMock(), closed=False)

        async def _run() -> None:
            await server._stream_chat_turn(
                "hi",
                ws,  # type: ignore[arg-type]
                "conn-1",
                bind_coara=fake,
                bind_ws_id="ws-a",
                subject="root",
            )

        task = asyncio.create_task(_run())
        await asyncio.sleep(0.5)  # chunk-1/2 已产出，进入静默期
        # ① 切走：detach 轮询检出后主迭代器提前 return
        root._web_view_workspace_id = "ws-b"
        await asyncio.sleep(0.6)
        # 主循环已退出并跑完 finally ——但 sender 必须仍在注册表里
        sender = root.end_registry.sender_for("web", fake.session_id)
        assert sender is not None, "切空间后 web sender 被提前注销（chunk 将无路由丢弃）"
        # ② 切回：drain 中的回合恢复产出——chunk 必须仍有确定路由
        root._web_view_workspace_id = "ws-a"
        release_tail.set()
        await asyncio.wait_for(task, timeout=3.0)
        # 回合结束后 finally 正常注销
        assert root.end_registry.sender_for("web", fake.session_id) is None
        # 关键断言：切走期间 drain 产出的尾部 chunk 已进本回合 buffer——
        # 切回/刷新重放可接续
        stream = next(iter(server._turns.values()))
        texts = [f.get("text") for f in stream.replay() if f.get("type") == "chunk"]
        assert any("tail-after-switch-back" in t for t in texts)
    finally:
        web_mod.turn = orig_turn

@pytest.mark.asyncio
async def test_chat_turn_tape_stays_on_origin_after_view_switch(tmp_path: Path) -> None:
    """P0-6: turn persist is pinned at start; switching the live view must not retarget tape."""
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    ws_a = tmp_path / "alpha"
    ws_b = tmp_path / "beta"
    ws_a.mkdir()
    ws_b.mkdir()

    store = shared_view_store()
    server = WebServer.__new__(WebServer)
    server.coara_home = coara_home
    server.workspace_dir = ws_a
    server._view_store = store
    server._bind_view_store(ws_a)
    server.registry = SimpleNamespace(
        has_active=lambda: False,
        send_to_active=lambda _m: None,
        send_to_active_nowait=lambda _m: None,
    )

    bind = SimpleNamespace(session_id="sess-a", workspace_dir=ws_a)
    tape_ws = str(bind.workspace_dir)
    stream = TurnStream(
        "turn-a",
        "web",
        "root",
        server,
        session_id=bind.session_id,
        workspace_dir=tape_ws,
    )
    # Live view moves to B (落带按帧自身的 workspace 归属，不随视图切换改线)。
    server.workspace_dir = ws_b
    server._bind_view_store(ws_b)

    stream.emit("user_message", content="hello from A")
    stream.emit("chunk", text="reply on A")
    await asyncio.sleep(0.05)
    store.flush(timeout=2.0)

    path_a = resolve_web_view_path(ws_a, coara_home=coara_home, subject="root", session_id="sess-a")
    path_b = resolve_web_view_path(ws_b, coara_home=coara_home, subject="root", session_id="sess-a")
    frames_a = WebViewStore.iter_frames(path_a)
    frames_b = WebViewStore.iter_frames(path_b) if path_b.exists() else []
    kinds_a = [f.get("kind") for f in frames_a]
    assert "user_message" in kinds_a
    assert "chunk" in kinds_a
    assert frames_b == [], "tape must not land on the post-switch view workspace"
    store.close()
