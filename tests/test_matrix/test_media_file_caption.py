"""Matrix 文件入站：端上附言必须随文件进回合。

回归背景：手机端「文件 + 附言」发出的 m.file 事件把附言放在 content.body，
入站此前只拼 ``发送了文件: @path``，整段附言被丢弃——用户填好的表格说明
到内核只剩一个文件名。修复后附言缀在文件行之后；端上无附言时 body 回落
成文件名（body == filename），那种情况不当作正文。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nio import DownloadResponse

import src.matrix_client.media_inbound as media_mod


class _FakeTurn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _event(*, body: str, filename: str | None) -> SimpleNamespace:
    content: dict[str, object] = {"msgtype": "m.file", "body": body}
    if filename is not None:
        content["filename"] = filename
    return SimpleNamespace(
        url="mxc://coara.local/abc",
        body=body,
        sender="@phone:coara.local",
        source={"content": content},
    )


async def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: SimpleNamespace) -> str:
    client = SimpleNamespace(download=AsyncMock(return_value=DownloadResponse(b"data", "application/pdf", "x.pdf")))
    room = SimpleNamespace(room_id="!room:coara.local", user_name=lambda s: s)
    root = SimpleNamespace(
        coara_home=None,
        matrix_view_workspace_id="ws-1",
        record_user_activity=lambda **kw: None,
    )
    file_bridge = SimpleNamespace(set_current_room=lambda rid: None)
    captured: list[str] = []

    async def _fake_stream(_root: object, message: str, **kwargs: object) -> None:
        captured.append(message)

    async def _send(rid: str, body: str) -> bool:
        return True

    monkeypatch.setattr(media_mod, "turn", lambda *a, **k: _FakeTurn())
    monkeypatch.setattr(media_mod, "stream_coara_reply_to_matrix", _fake_stream)

    await media_mod.process_matrix_media_inbound(
        client,
        root,
        room,
        event,
        workspace_dir=tmp_path,
        trust_level="owner",
        file_bridge=file_bridge,
        send_chunk=_send,
        send_room_text=_send,
    )
    return captured[0]


@pytest.mark.asyncio
async def test_file_caption_reaches_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _run(tmp_path, monkeypatch, _event(body="填一下表格，附言必须到内核", filename="报表.pdf"))
    assert text.startswith("发送了文件")
    assert str((tmp_path / "uploads" / "报表.pdf").resolve()) in text
    assert "填一下表格，附言必须到内核" in text


@pytest.mark.asyncio
async def test_filename_body_is_not_treated_as_caption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _run(tmp_path, monkeypatch, _event(body="报表.pdf", filename="报表.pdf"))
    assert text == f"发送了文件（用户附件，已存入工作空间 uploads/）: {(tmp_path / 'uploads' / '报表.pdf').resolve()}"
