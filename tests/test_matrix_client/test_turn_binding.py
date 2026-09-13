"""Schedule-time session binding for Matrix inbound turns.

A message queued behind a busy session's dispatcher lock must still run on
the workspace the user was looking at when they sent it — even if a ``/ws``
switch happens while it waits. Same for leftover continuation drains.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.types import ContinuationInput
from src.matrix_client.inbound_handlers import (
    MatrixInboundHost,
    process_matrix_media_message,
    process_matrix_text_message,
)


class _FakeCoara:
    def __init__(self, session_id: str, workspace_dir: str, chunks: list[str]) -> None:
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self._chunks = chunks
        self.calls = 0
        self._continuation_inputs: list[ContinuationInput] = []
        self.identity = SimpleNamespace(coara_id=f"id-{session_id}", name="考拉")

    async def process_message(self, *_args, **_kwargs):
        self.calls += 1
        for chunk in self._chunks:
            yield chunk

    def drain_continuation_inputs(self) -> list[ContinuationInput]:
        items = list(self._continuation_inputs)
        self._continuation_inputs.clear()
        return items


def _make_root(fg: _FakeCoara, fg_ws_id: str):
    return SimpleNamespace(
        foreground_coara=fg,
        _foreground_session_id=fg_ws_id,
        event_bus=SimpleNamespace(publish=lambda *_a, **_k: None),
        identity=SimpleNamespace(coara_id="root-id", name="考拉"),
        record_user_activity=lambda **_kwargs: None,
        workspace_manager=None,
    )


def _make_host(root) -> tuple[MatrixInboundHost, list[str]]:
    sent: list[str] = []

    async def send_chunk(_room: str, body: str) -> None:
        sent.append(body)

    host = MatrixInboundHost(
        root=root,
        client=MagicMock(),
        file_bridge=MagicMock(),
        coara_home=None,
        cli_owner=True,
        send_chunk=send_chunk,
        send_text=AsyncMock(),
        send_room_text=AsyncMock(),
        report_text_error=AsyncMock(),
        report_media_error=AsyncMock(),
    )
    return host, sent


@pytest.fixture
def _patch_ingress(monkeypatch: pytest.MonkeyPatch):
    @contextlib.asynccontextmanager
    async def _dummy_cm(*_args, **_kwargs):
        yield

    monkeypatch.setattr("src.matrix_client.inbound_handlers.matrix_turn_scope", _dummy_cm)
    monkeypatch.setattr("src.matrix_client.inbound_handlers.bind_matrix_active_room", lambda **_kw: None)
    monkeypatch.setattr("src.matrix_client.ingress_helpers.turn", _dummy_cm)


@pytest.mark.asyncio
async def test_text_turn_and_leftover_stay_on_bound_session(_patch_ingress) -> None:
    """Queued behind A's lock; foreground moved to B before execution —
    the turn and its leftover continuations must still run on A."""
    a = _FakeCoara("sa", "D:/ws/alpha", ["A回复"])
    b = _FakeCoara("sb", "D:/ws/beta", ["B回复"])
    a._continuation_inputs = [ContinuationInput(text="追问")]
    root = _make_root(b, "ws-b")
    host, sent = _make_host(root)

    room = SimpleNamespace(room_id="!r")
    event = SimpleNamespace(body="hello", sender="@u:x")
    await process_matrix_text_message(host, room, event, bind_coara=a, bind_ws_id="ws-a")

    assert b.calls == 0
    # Initial message + drained leftover continuation, both on A.
    assert a.calls == 2
    # Detached chunks carry the origin workspace tag.
    assert any(s.startswith("[alpha]") for s in sent)


@pytest.mark.asyncio
async def test_text_without_binding_falls_back_to_foreground(_patch_ingress) -> None:
    a = _FakeCoara("sa", "D:/ws/alpha", ["A回复"])
    b = _FakeCoara("sb", "D:/ws/beta", ["B回复"])
    root = _make_root(b, "ws-b")
    host, _sent = _make_host(root)

    room = SimpleNamespace(room_id="!r")
    event = SimpleNamespace(body="hello", sender="@u:x")
    await process_matrix_text_message(host, room, event)

    assert b.calls == 1
    assert a.calls == 0


@pytest.mark.asyncio
async def test_media_uploads_to_bound_workspace(_patch_ingress, tmp_path: Path) -> None:
    inbound = AsyncMock()
    import src.matrix_client.media_inbound as media_mod

    orig = media_mod.process_matrix_media_inbound
    media_mod.process_matrix_media_inbound = inbound
    try:
        a = _FakeCoara("sa", str(tmp_path / "alpha"), [])
        b = _FakeCoara("sb", str(tmp_path / "beta"), [])
        root = _make_root(b, "ws-b")
        host, _sent = _make_host(root)

        room = SimpleNamespace(room_id="!r")
        # 非图片文件走本路径；图片自 09-07 起在回调入口聚合，不经此 handler
        event = SimpleNamespace(body="report.pdf", sender="@u:x")
        await process_matrix_media_message(host, room, event, bind_coara=a)

        assert inbound.await_count == 1
        assert inbound.await_args.kwargs["workspace_dir"] == Path(a.workspace_dir)
    finally:
        media_mod.process_matrix_media_inbound = orig


@pytest.mark.asyncio
async def test_media_turn_drains_leftover_continuations(_patch_ingress, tmp_path: Path) -> None:
    """媒体回合收尾后遗留的接续输入（回合末段才到达的文本）在 handler 内排空，
    不再 stalled 到下一条入站消息；排空仍跑在调度时绑定的会话上"""
    inbound = AsyncMock()
    import src.matrix_client.media_inbound as media_mod

    orig = media_mod.process_matrix_media_inbound
    media_mod.process_matrix_media_inbound = inbound
    try:
        a = _FakeCoara("sa", str(tmp_path / "alpha"), ["追问回复"])
        b = _FakeCoara("sb", str(tmp_path / "beta"), [])
        a._continuation_inputs = [ContinuationInput(text="媒体回合途中发来的追问")]
        root = _make_root(b, "ws-b")
        host, _sent = _make_host(root)

        room = SimpleNamespace(room_id="!r")
        # 同上：非图片文件才走 process_matrix_media_inbound
        event = SimpleNamespace(body="report.pdf", sender="@u:x")
        await process_matrix_media_message(host, room, event, bind_coara=a, bind_ws_id="ws-a")

        assert inbound.await_count == 1
        # 遗留接续输入被排空并投递到绑定会话 a（前台 b 不被触碰）
        assert a.calls == 1
        assert b.calls == 0
        assert not a._continuation_inputs
    finally:
        media_mod.process_matrix_media_inbound = orig
