"""Tests for Matrix room_send guard helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nio import RoomSendResponse
from nio.responses import RoomSendError

from src.matrix_client.send_guard import matrix_room_send_text


@pytest.mark.asyncio
async def test_matrix_room_send_text_success() -> None:
    client = MagicMock(logged_in=True)
    client.room_send = AsyncMock(
        return_value=RoomSendResponse(event_id="$evt", room_id="!r:local"),
    )

    ok = await matrix_room_send_text(client, "!r:local", "hello")

    assert ok is True
    client.room_send.assert_awaited_once()
    # turn 输出打 intermediate 标志：手机端凭 coara_turn 判断 typing 是否继续
    content = client.room_send.await_args.kwargs.get("content") or client.room_send.await_args.args[2]
    assert content["coara_turn"] == "intermediate"


def test_turn_end_envelope_content_flagged_final() -> None:
    from src.matrix_client.send_guard import _build_text_content
    from src.matrix_client.turn_signal import MATRIX_TURN_QUIET_ENVELOPE, turn_end_envelope

    assert _build_text_content(turn_end_envelope(background=False))["coara_turn"] == "final"
    assert _build_text_content(MATRIX_TURN_QUIET_ENVELOPE)["coara_turn"] == "final"
    assert _build_text_content("普通输出")["coara_turn"] == "intermediate"


@pytest.mark.asyncio
async def test_matrix_room_send_text_rejects_room_send_error() -> None:
    client = MagicMock(logged_in=True)
    client.room_send = AsyncMock(
        return_value=RoomSendError(message="user not joined", status_code="M_FORBIDDEN"),
    )

    ok = await matrix_room_send_text(client, "!r:local", "hello", max_attempts=1)

    assert ok is False


@pytest.mark.asyncio
async def test_matrix_room_send_text_rejoins_after_forbidden() -> None:
    client = MagicMock(logged_in=True)
    client.room_send = AsyncMock(
        side_effect=[
            RoomSendError(message="user not joined", status_code="M_FORBIDDEN"),
            RoomSendResponse(event_id="$evt", room_id="!r:local"),
        ],
    )
    ensure = AsyncMock(return_value=True)

    ok = await matrix_room_send_text(
        client,
        "!r:local",
        "hello",
        max_attempts=2,
        ensure_joined=ensure,
    )

    assert ok is True
    ensure.assert_awaited_once()
    assert client.room_send.await_count == 2


@pytest.mark.asyncio
async def test_matrix_room_send_text_retries_transient_error() -> None:
    client = MagicMock(logged_in=True)
    client.room_send = AsyncMock(
        side_effect=[
            RoomSendError(message="timeout", status_code=504),
            RoomSendResponse(event_id="$evt", room_id="!r:local"),
        ],
    )

    ok = await matrix_room_send_text(client, "!r:local", "hello", max_attempts=2)

    assert ok is True
    assert client.room_send.await_count == 2


@pytest.mark.asyncio
async def test_matrix_room_send_text_retries_database_locked() -> None:
    client = MagicMock(logged_in=True)
    client.room_send = AsyncMock(
        side_effect=[
            RoomSendError(
                message="insert event: database is locked (517)",
                status_code="M_FORBIDDEN",
            ),
            RoomSendResponse(event_id="$evt", room_id="!r:local"),
        ],
    )

    ok = await matrix_room_send_text(client, "!r:local", "hello", max_attempts=2)

    assert ok is True
    assert client.room_send.await_count == 2


@pytest.mark.asyncio
async def test_matrix_room_send_text_reuses_tx_id_across_retries() -> None:
    """All retry attempts of one logical send must share one txn id."""
    client = MagicMock(logged_in=True)
    client.room_send = AsyncMock(
        side_effect=[
            RoomSendError(message="timeout", status_code=504),
            RoomSendError(message="timeout", status_code=504),
            RoomSendResponse(event_id="$evt", room_id="!r:local"),
        ],
    )

    ok = await matrix_room_send_text(client, "!r:local", "hello", max_attempts=3)

    assert ok is True
    tx_ids = [call.kwargs["tx_id"] for call in client.room_send.await_args_list]
    assert len(tx_ids) == 3
    assert len(set(tx_ids)) == 1
    assert tx_ids[0]
