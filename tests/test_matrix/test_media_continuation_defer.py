"""Matrix 图片消息忙时接续入队：caption 与 image_blocks 绑在同一队列项。

回归背景：媒体入站此前没有忙时检查，忙时直闯 process_message 堵在回合锁外，
caption 文本经别的路径入队、image_blocks 随被丢弃的协程消失——用户接续发图
LLM 永远看不到。修复后：有活跃回合时图片经 try_defer_media_to_continuation_input
入队，由回合迭代头 drain 组多模态注入。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.types import CoaraStatus
from src.matrix_client.ingress_helpers import try_defer_media_to_continuation_input
from tests.helpers import make_test_coara


def _image_block() -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


@pytest.mark.asyncio
async def test_media_deferred_with_images_when_turn_active(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    await coara.initialize()
    coara._active_turn = SimpleNamespace(turn_id="t1")
    coara.status = CoaraStatus.RUNNING

    root = SimpleNamespace(end_registry=None, foreground_coara=coara)
    sent: list[str] = []

    async def send_text(room_id: str, body: str) -> bool:
        sent.append(body)
        return True

    ok = try_defer_media_to_continuation_input(
        root,
        "看看这张图",
        [_image_block()],
        room_id="!room:x",
        send_text=send_text,
        interaction_channel=None,
    )
    assert ok is True

    items = coara.drain_continuation_inputs()
    assert len(items) == 1
    assert items[0].text == "看看这张图"
    assert items[0].image_blocks == [_image_block()]
    assert items[0].source == "matrix"


@pytest.mark.asyncio
async def test_media_not_deferred_when_idle(tmp_path: Path) -> None:
    """空闲时不入队——走正常直进回合路径。"""
    coara = make_test_coara(tmp_path)
    await coara.initialize()

    root = SimpleNamespace(end_registry=None, foreground_coara=coara)
    ok = try_defer_media_to_continuation_input(
        root,
        "图",
        [_image_block()],
        room_id="!room:x",
        send_text=None,
    )
    assert ok is False
    assert coara.drain_continuation_inputs() == []


@pytest.mark.asyncio
async def test_media_not_deferred_without_images(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    await coara.initialize()
    coara._active_turn = SimpleNamespace(turn_id="t1")
    coara.status = CoaraStatus.RUNNING

    root = SimpleNamespace(end_registry=None, foreground_coara=coara)
    ok = try_defer_media_to_continuation_input(root, "纯文字不该走这", [], room_id="!room:x")
    assert ok is False
