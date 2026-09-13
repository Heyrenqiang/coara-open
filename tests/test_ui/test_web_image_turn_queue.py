"""带图消息 mid-turn 接续（与 CLI Plan B 对齐）。"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.core.types import ContinuationInput
from src.ui.web_server import WebServer


def _permit_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    # 无 provider 早失败检查在隔离测试环境会拦掉回合（无 key），桩成总有可用 provider
    monkeypatch.setattr("src.llm.model_catalog._enabled_provider_names", lambda _mgr: ["fake"])


class _FakeCoara:
    """记录回合执行顺序的假前台 session"""

    def __init__(self, session_id: str, workspace_dir: Path) -> None:
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self._active = False
        self._active_turn: Any | None = None
        self._continuation_inputs: list[ContinuationInput] = []
        self.turns: list[tuple[str, object]] = []
        self.drained_mid_turn: list[ContinuationInput] = []
        self.mirror_ctx: tuple | None = None
        self.first_turn_started = asyncio.Event()
        self.release_first_turn = asyncio.Event()

    def has_active_turn(self) -> bool:
        return self._active

    def set_deferred_remote_ctx(
        self,
        channel_id: str,
        send_text: object,
        interaction_channel: object,
        *,
        source: str = "",
        actor: str = "",
    ) -> None:
        self.mirror_ctx = (channel_id, send_text, interaction_channel)

    def submit_continuation_input(
        self,
        text: str,
        image_blocks: list[dict] | None = None,
        *,
        source: str = "",
    ) -> None:
        self._continuation_inputs.append(ContinuationInput(text=text, image_blocks=image_blocks, source=source))

    def drain_continuation_inputs(self) -> list[ContinuationInput]:
        items = list(self._continuation_inputs)
        self._continuation_inputs.clear()
        return items

    async def process_message(self, content, **kwargs):
        self._active = True
        self.turns.append((content, kwargs.get("image_blocks")))
        try:
            if len(self.turns) == 1:
                self.first_turn_started.set()
                # 首轮回合挂住，等测试把接续输入与带图消息都排好
                await self.release_first_turn.wait()
                # 模拟真实回合循环：迭代点 drain 接续输入（turn_orchestrator 语义）
                self.drained_mid_turn.extend(self.drain_continuation_inputs())
            yield "chunk"
        finally:
            self._active = False


@pytest.mark.asyncio
async def test_image_message_injects_mid_turn_continuation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """活跃回合期间：纯文本与带图跟话均注入接续队列（CLI Plan B），不新开独立回合。"""
    _permit_providers(monkeypatch)
    coara = _FakeCoara("sa", tmp_path)
    root = SimpleNamespace(foreground_coara=coara, _foreground_session_id="ws-a")

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
        server._web_followup_view_turns = set()
        server._view_store = None
        server.workspace_dir = tmp_path
        server.coara_home = None
        server.registry = SimpleNamespace(
            has_active=lambda: False,
            send_to_active=AsyncMock(return_value=False),
            send_to_active_nowait=lambda _msg: None,
        )
        server._resolve_image_refs = AsyncMock(return_value=[{"type": "image", "source": "ref"}])

        ws = SimpleNamespace(send_str=AsyncMock(), closed=False)
        main_task = asyncio.create_task(server._handle_chat({"text": "首轮"}, ws, "conn-1"))
        await coara.first_turn_started.wait()

        cont_task = asyncio.create_task(server._handle_chat({"text": "接续一"}, ws, "conn-1"))
        await asyncio.wait_for(cont_task, timeout=2)
        assert len(coara._continuation_inputs) == 1
        # 入站存裸文本，来源标签由内核按 source=web 现包（turn_orchestrator 接续现包）
        assert coara._continuation_inputs[0].text == "接续一"
        assert coara.mirror_ctx is not None and coara.mirror_ctx[0] == "conn-1"

        image_task = asyncio.create_task(server._handle_chat({"text": "看图", "image_refs": ["a.png"]}, ws, "conn-1"))
        await asyncio.wait_for(image_task, timeout=2)

        assert len(coara._continuation_inputs) == 2
        assert coara._continuation_inputs[1].image_blocks == [{"type": "image", "source": "ref"}]
        assert coara._continuation_inputs[1].text == "看图"

        coara.release_first_turn.set()
        await asyncio.wait_for(main_task, timeout=2)

        # 仅首轮作为独立 turn；带图已在 mid-turn drain
        assert [text for text, _ in coara.turns] == ["首轮"]
        assert len(coara.drained_mid_turn) == 2
        assert coara.drained_mid_turn[1].image_blocks == [{"type": "image", "source": "ref"}]
        assert server._turn_tails == {}
    finally:
        web_mod.turn = orig_turn


@pytest.mark.asyncio
async def test_consecutive_web_messages_second_injects_mid_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续两条 Web 消息：第二条在首轮活跃期间注入接续队列（同手机端），
    不新开回合——turn_start/turn_end 只有一对，回复接续在同一条流里。"""

    _permit_providers(monkeypatch)
    coara = _FakeCoara("sa", tmp_path)
    root = SimpleNamespace(foreground_coara=coara, _foreground_session_id="ws-a")

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
        server._web_followup_view_turns = set()
        server._view_store = None
        server.workspace_dir = tmp_path
        server.coara_home = None
        server._resolve_image_refs = AsyncMock(return_value=[])

        sent: list[dict] = []

        # 帧经 TurnStream → registry 广播（不再直发 ws）：registry stub 捕获帧。
        def _nowait(msg: dict) -> None:
            sent.append(msg)

        server.registry = SimpleNamespace(
            has_active=lambda: True,
            send_to_active=AsyncMock(return_value=True),
            send_to_active_nowait=_nowait,
        )

        ws = SimpleNamespace(send_str=AsyncMock(), closed=False)

        first_task = asyncio.create_task(server._handle_chat({"text": "a"}, ws, "conn-1"))
        await coara.first_turn_started.wait()
        second_task = asyncio.create_task(server._handle_chat({"text": "b"}, ws, "conn-1"))
        await asyncio.wait_for(second_task, timeout=2)

        # 第二条已注入 未开新回合
        assert len(coara._continuation_inputs) == 1
        coara.release_first_turn.set()
        await asyncio.wait_for(first_task, timeout=2)

        markers = [m["type"] for m in sent if m["type"] in ("turn_start", "turn_end")]
        assert markers == ["turn_start", "turn_end"]
        assert [text for text, _ in coara.turns] == ["a"]
        assert server._turn_tails == {}
    finally:
        web_mod.turn = orig_turn


class _FakeCoaraNoActiveTurn(_FakeCoara):
    """首回合不置 active——第二条消息经 _turn_tails 排队（而非 mid-turn 接续）。"""

    async def process_message(self, content, **kwargs):
        self.turns.append((content, kwargs.get("image_blocks")))
        try:
            if len(self.turns) == 1:
                self.first_turn_started.set()
                await self.release_first_turn.wait()
            yield "chunk"
        finally:
            pass


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_queued_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回合与连接解耦：WS 断连不再取消排队/进行中的回合（TurnStream 语义）。
    服务端 finally 只清 task 跟踪表，回合任务继续跑完并输出进 buffer。"""
    _permit_providers(monkeypatch)
    coara = _FakeCoaraNoActiveTurn("sa", tmp_path)
    root = SimpleNamespace(foreground_coara=coara, _foreground_session_id="ws-a")

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
        server._web_followup_view_turns = set()
        server._view_store = None
        server.workspace_dir = tmp_path
        server.coara_home = None
        server._chat_tasks = {"conn-1": set()}
        server._resolve_image_refs = AsyncMock(return_value=[])
        server.registry = SimpleNamespace(
            has_active=lambda: False,
            send_to_active=AsyncMock(return_value=False),
            send_to_active_nowait=lambda _msg: None,
        )

        ws = SimpleNamespace(send_str=AsyncMock(), closed=False)
        first_task = asyncio.create_task(server._handle_chat({"text": "a"}, ws, "conn-1"))
        await coara.first_turn_started.wait()

        # 第二条消息：has_active_turn() 为 False → 走 _turn_tails 排队
        second_task = asyncio.create_task(server._handle_chat({"text": "b"}, ws, "conn-1"))
        await asyncio.sleep(0.05)  # 让第二条进入 await prev_tail

        # 模拟 WS 断连：只清 task 跟踪表，不取消回合任务（新语义）。
        server._chat_tasks.pop("conn-1", None)

        # 两回合照常完成：消息不丢、不需要 continuation 兜底。
        coara.release_first_turn.set()
        await asyncio.wait_for(first_task, timeout=2)
        await asyncio.wait_for(second_task, timeout=2)
        assert [text for text, _ in coara.turns] == ["a", "b"]
        assert coara._continuation_inputs == []
        assert server._turn_tails == {}
    finally:
        web_mod.turn = orig_turn
