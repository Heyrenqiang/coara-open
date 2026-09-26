"""stream_coara_reply_to_matrix must open Matrix turn context when callers omit turn()."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.coara.turn_context import get_end_channel, get_turn_channel, get_turn_channel_id
from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL
from src.matrix_client.response_stream import _ensure_matrix_turn_context, _matrix_turn_context_active


@pytest.mark.asyncio
async def test_ensure_matrix_turn_context_opens_when_missing() -> None:
    """裸调用路径（leftover / cross-end）无 turn 时必须补上，否则审批找不到房间。"""
    assert not _matrix_turn_context_active("!room:x")
    chunks: list[tuple[str, str]] = []

    async def send_chunk(rid: str, body: str) -> None:
        chunks.append((rid, body))

    async with _ensure_matrix_turn_context("!room:x", send_chunk):
        assert get_turn_channel_id() == "!room:x"
        assert get_turn_channel() is MATRIX_REMOTE_INTERACTION_CHANNEL
        end = get_end_channel()
        assert end is not None
        assert end.source == "matrix"
        assert end.channel_id == "!room:x"

    assert get_turn_channel_id() is None
    assert get_turn_channel() is None


@pytest.mark.asyncio
async def test_ensure_matrix_turn_context_nested_noop() -> None:
    """调用方已包 turn 时不覆盖现有 ContextVar（避免嵌套冲掉 send_text）。"""
    from src.coara.turn_context import turn

    async def outer_send(rid: str, body: str) -> bool:
        return True

    async def inner_chunk(rid: str, body: str) -> None:
        return None

    async with turn(
        "matrix",
        channel_id="!room:outer",
        send_text=outer_send,
        interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL,
    ), _ensure_matrix_turn_context("!room:outer", inner_chunk):
        assert get_turn_channel_id() == "!room:outer"
        assert get_end_channel() is not None
        assert get_end_channel().send_text is outer_send


@pytest.mark.asyncio
async def test_stream_without_outer_turn_still_sets_approval_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_coara_reply_to_matrix 在无外层 turn 时，process_message 期间仍有 matrix 通道。"""
    from src.matrix_client import response_stream

    seen: dict[str, object] = {}

    async def fake_process_message(self, *args, **kwargs):
        seen["channel_id"] = get_turn_channel_id()
        seen["channel"] = get_turn_channel()
        return
        yield  # pragma: no cover — make this an async generator

    class _Coara:
        session_id = "sess"
        workspace_dir = "/tmp"
        identity = SimpleNamespace(coara_id="c1", name="t")

        process_message = fake_process_message

    root = SimpleNamespace(
        foreground_coara=_Coara(),
        end_registry=None,
        identity=SimpleNamespace(coara_id="c1", name="t"),
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
        _foreground_session_id="ws1",
    )
    monkeypatch.setattr(
        "src.matrix_client.response_stream.try_handle_matrix_chat_command",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.coara.commands.report.try_consume_pending_report_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.matrix_client.ingress_helpers.matrix_view_coara",
        lambda r: root.foreground_coara,
    )
    monkeypatch.setattr(
        "src.matrix_client.ingress_helpers.matrix_view_session_key",
        lambda r: "ws1",
    )

    await response_stream.stream_coara_reply_to_matrix(
        root,
        "hello",
        room_id="!room:phone",
        trust_level="owner",
        send_chunk=AsyncMock(),
    )
    assert seen["channel_id"] == "!room:phone"
    assert seen["channel"] is MATRIX_REMOTE_INTERACTION_CHANNEL
