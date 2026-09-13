"""Tests for Matrix inbound media file handling (m.file uploads)."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from nio import DownloadResponse

from src.matrix_client import media_inbound
from src.matrix_client.media_inbound import MAX_MATRIX_UPLOAD_BYTES, process_matrix_media_inbound


def _make_event(filename: str) -> MagicMock:
    event = MagicMock()
    event.source = {"content": {"msgtype": "m.file", "filename": filename, "body": filename}}
    event.body = filename
    event.filename = filename
    event.sender = "@user:server"
    event.url = "mxc://server/file"
    return event


def _make_room() -> MagicMock:
    room = MagicMock()
    room.room_id = "!room:server"
    room.user_name = lambda _sender: "user"
    return room


def _patch_turn(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stream = AsyncMock()
    monkeypatch.setattr(media_inbound, "stream_coara_reply_to_matrix", stream)

    @contextlib.asynccontextmanager
    async def _dummy_remote_turn(*args, **kwargs):
        yield

    monkeypatch.setattr(media_inbound, "turn", _dummy_remote_turn)
    return stream


@pytest.mark.asyncio
async def test_upload_sanitizes_traversal_filename(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch)
    client = MagicMock()
    client.download = AsyncMock(return_value=DownloadResponse(body=b"data", content_type="text/plain", filename=None))
    send_room_text = AsyncMock()

    await process_matrix_media_inbound(
        client,
        MagicMock(),
        _make_room(),
        _make_event("../../evil.txt"),
        workspace_dir=tmp_path,
        trust_level="untrusted",
        file_bridge=MagicMock(),
        send_chunk=AsyncMock(),
        send_room_text=send_room_text,
    )

    saved = tmp_path / "uploads" / "evil.txt"
    assert saved.read_bytes() == b"data"
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.asyncio
async def test_upload_avoids_overwriting_existing_file(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "report.txt").write_bytes(b"old")
    client = MagicMock()
    client.download = AsyncMock(return_value=DownloadResponse(body=b"new", content_type="text/plain", filename=None))

    await process_matrix_media_inbound(
        client,
        MagicMock(),
        _make_room(),
        _make_event("report.txt"),
        workspace_dir=tmp_path,
        trust_level="untrusted",
        file_bridge=MagicMock(),
        send_chunk=AsyncMock(),
        send_room_text=AsyncMock(),
    )

    assert (uploads / "report.txt").read_bytes() == b"old"
    assert (uploads / "report_1.txt").read_bytes() == b"new"


@pytest.mark.asyncio
async def test_image_upload_keeps_empty_caption(tmp_path, monkeypatch) -> None:
    """空 caption 的图片批次投递：不合成「请描述」占位文本，image_blocks 原样到 stream 层。

    图片自批量聚合起不再经 process_matrix_media_inbound，本用例测聚合批次的
    最终投递点 _deliver_image_turn。
    """
    from src.matrix_client import ingress_helpers
    from src.matrix_client.media_inbound import _deliver_image_turn

    stream = _patch_turn(monkeypatch)
    monkeypatch.setattr(
        ingress_helpers,
        "try_defer_media_to_continuation_input",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(ingress_helpers, "matrix_view_session_key", lambda _r: "")

    await _deliver_image_turn(
        MagicMock(),
        "!room:server",
        "",
        [{"type": "image"}],
        trust_level="untrusted",
        send_chunk=AsyncMock(),
        echo_tool_summary_local=None,
        bind_coara=None,
        bind_ws_id=None,
    )

    remote_body = stream.await_args.args[1]
    assert remote_body == ""
    assert "请描述" not in remote_body
    assert stream.await_args.kwargs.get("image_blocks") == [{"type": "image"}]


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch)
    client = MagicMock()
    client.download = AsyncMock(
        return_value=DownloadResponse(
            body=b"x" * (MAX_MATRIX_UPLOAD_BYTES + 1),
            content_type="application/octet-stream",
            filename=None,
        )
    )
    send_room_text = AsyncMock()

    await process_matrix_media_inbound(
        client,
        MagicMock(),
        _make_room(),
        _make_event("big.bin"),
        workspace_dir=tmp_path,
        trust_level="untrusted",
        file_bridge=MagicMock(),
        send_chunk=AsyncMock(),
        send_room_text=send_room_text,
    )

    send_room_text.assert_awaited_once()
    assert not (tmp_path / "uploads").exists() or not list((tmp_path / "uploads").iterdir())
