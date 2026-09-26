"""Matrix 图片入站落盘：图片既组视觉块又按文件通道规则存 uploads/。

回归背景：手机端「相册发图」走的图片通道此前只把图转成 base64 视觉块喂模型，
用完即弃、不落盘——用户事后无法在空间找到附件，模型也没法 read 原图。
修复后图片本体与 m.file 同一套规则落盘 uploads/，落点绝对路径注入消息文本。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.matrix_client.media_inbound as media_mod


def _png_bytes() -> bytes:
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def _image_event(*, body: str = "", filename: str | None = "photo.jpg") -> SimpleNamespace:
    content: dict[str, object] = {"msgtype": "m.image", "body": body, "info": {"mimetype": "image/jpeg"}}
    if filename is not None:
        content["filename"] = filename
    return SimpleNamespace(
        url="mxc://coara.local/img1",
        body=body,
        sender="@phone:coara.local",
        source={"content": content},
    )


def _root(workspace_dir: Path) -> SimpleNamespace:
    fg = SimpleNamespace(workspace_dir=str(workspace_dir), provider_name="", model_name="")
    return SimpleNamespace(
        foreground_coara=fg,
        workspace_dir=str(workspace_dir),
        coara_home=None,
        record_user_activity=lambda **kw: None,
        view_workspace_id=lambda: "ws-1",
        pinned_view_id=lambda: None,
    )


def _room() -> SimpleNamespace:
    return SimpleNamespace(room_id="!room:coara.local", user_name=lambda s: s)


@pytest.mark.asyncio
async def test_image_saved_to_uploads_and_path_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delivered: list[tuple] = []

    async def _fake_blocks(client, *, mxc_url, mime_type, quoted_mxc=None, declared_name=None, originals=None):
        if originals is not None:
            originals.append({"data": _png_bytes(), "mime_type": "image/jpeg", "declared_name": declared_name})
        return [{"type": "image"}]

    monkeypatch.setattr(media_mod, "build_image_blocks_from_matrix_upload", _fake_blocks)

    from src.matrix_client import media_batch

    agg = media_batch.MatrixMediaBatchAggregator()
    monkeypatch.setattr(media_batch, "media_batch_aggregator", agg)

    handler = media_mod.build_media_batch_handler(
        MagicMock(),
        _root(tmp_path),
        trust_level="owner",
        send_room_text=AsyncMock(),
        deliver_batch=lambda rid, blocks, text, saved: delivered.append((rid, blocks, text, saved)),
    )
    await handler(_room(), _image_event(body="看看这张", filename="photo.jpg"))
    agg.flush_now("!room:coara.local")

    saved_file = tmp_path / "uploads" / "photo.jpg"
    assert saved_file.read_bytes() == _png_bytes()
    assert len(delivered) == 1
    _rid, blocks, text, saved = delivered[0]
    assert blocks == [{"type": "image"}]
    assert saved and saved[0]["path"].endswith("photo.jpg")
    # 落点注入由 _deliver_image_turn 在投递时完成（此处验证 saved 已随批次携带）
    assert text == "看看这张"


@pytest.mark.asyncio
async def test_image_default_name_when_no_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delivered: list[tuple] = []

    async def _fake_blocks(client, *, mxc_url, mime_type, quoted_mxc=None, declared_name=None, originals=None):
        if originals is not None:
            originals.append({"data": _png_bytes(), "mime_type": "image/jpeg", "declared_name": declared_name})
        return [{"type": "image"}]

    monkeypatch.setattr(media_mod, "build_image_blocks_from_matrix_upload", _fake_blocks)
    from src.matrix_client import media_batch

    agg = media_batch.MatrixMediaBatchAggregator()
    monkeypatch.setattr(media_batch, "media_batch_aggregator", agg)

    handler = media_mod.build_media_batch_handler(
        MagicMock(),
        _root(tmp_path),
        trust_level="owner",
        send_room_text=AsyncMock(),
        deliver_batch=lambda rid, blocks, text, saved: delivered.append((rid, blocks, text, saved)),
    )
    await handler(_room(), _image_event(filename=None))
    agg.flush_now("!room:coara.local")

    files = list((tmp_path / "uploads").iterdir())
    assert len(files) == 1 and files[0].suffix == ".jpg"


@pytest.mark.asyncio
async def test_image_persist_failure_does_not_block_vision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delivered: list[tuple] = []

    async def _fake_blocks(client, *, mxc_url, mime_type, quoted_mxc=None, declared_name=None, originals=None):
        if originals is not None:
            originals.append({"data": _png_bytes(), "mime_type": "image/jpeg", "declared_name": "photo.jpg"})
        return [{"type": "image"}]

    monkeypatch.setattr(media_mod, "build_image_blocks_from_matrix_upload", _fake_blocks)
    monkeypatch.setattr(media_mod, "_save_uploads", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    from src.matrix_client import media_batch

    agg = media_batch.MatrixMediaBatchAggregator()
    monkeypatch.setattr(media_batch, "media_batch_aggregator", agg)

    handler = media_mod.build_media_batch_handler(
        MagicMock(),
        _root(tmp_path),
        trust_level="owner",
        send_room_text=AsyncMock(),
        deliver_batch=lambda rid, blocks, text, saved: delivered.append((rid, blocks, text, saved)),
    )
    await handler(_room(), _image_event(filename="photo.jpg"))
    agg.flush_now("!room:coara.local")

    # 落盘失败不阻塞：视觉块照常投递，saved 为空不注入落点
    assert len(delivered) == 1
    assert delivered[0][1] == [{"type": "image"}]
    assert delivered[0][3] == []
