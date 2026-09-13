"""Matrix 图片批量聚合器：窗口合并、文本冲刷保序、数量上限、投递失败不炸。"""

from __future__ import annotations

import asyncio

import pytest

from src.matrix_client import media_batch
from src.matrix_client.media_batch import MatrixMediaBatchAggregator


def _img(tag: str) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tag}"}}


@pytest.fixture(autouse=True)
def _fast_window(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COARA_MATRIX_MEDIA_BATCH_MS", "80")
    yield
    media_batch.media_batch_aggregator._batches.clear()


@pytest.mark.asyncio
async def test_images_in_window_merge_into_one_batch() -> None:
    agg = MatrixMediaBatchAggregator()
    delivered: list[tuple[list, str]] = []
    agg.add("!r:x", blocks=[_img("1")], caption="", deliver=lambda b, t: delivered.append((b, t)))
    await asyncio.sleep(0.02)
    agg.add("!r:x", blocks=[_img("2")], caption="", deliver=lambda b, t: delivered.append((b, t)))
    await asyncio.sleep(0.02)
    agg.add("!r:x", blocks=[_img("3")], caption="三张图", deliver=lambda b, t: delivered.append((b, t)))
    await asyncio.sleep(0.2)
    assert len(delivered) == 1
    blocks, text = delivered[0]
    assert len(blocks) == 3
    assert text == "三张图"


@pytest.mark.asyncio
async def test_flush_now_delivers_immediately_for_text_ordering() -> None:
    agg = MatrixMediaBatchAggregator()
    delivered: list[tuple[list, str]] = []
    agg.add("!r:x", blocks=[_img("1")], caption="", deliver=lambda b, t: delivered.append((b, t)))
    # 文本到达：立即冲刷，不等窗口
    agg.flush_now("!r:x")
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_captions_join_in_arrival_order() -> None:
    agg = MatrixMediaBatchAggregator()
    delivered: list[tuple[list, str]] = []
    deliver = lambda b, t: delivered.append((b, t))  # noqa: E731
    agg.add("!r:x", blocks=[_img("1")], caption="先看这张", deliver=deliver)
    agg.add("!r:x", blocks=[_img("2")], caption="再看这张", deliver=deliver)
    agg.flush_now("!r:x")
    assert delivered[0][1] == "先看这张\n再看这张"


@pytest.mark.asyncio
async def test_rooms_are_independent() -> None:
    agg = MatrixMediaBatchAggregator()
    delivered_a: list[tuple[list, str]] = []
    delivered_b: list[tuple[list, str]] = []
    agg.add("!a:x", blocks=[_img("1")], caption="", deliver=lambda b, t: delivered_a.append((b, t)))
    agg.add("!b:x", blocks=[_img("2")], caption="", deliver=lambda b, t: delivered_b.append((b, t)))
    agg.flush_now("!a:x")
    assert len(delivered_a) == 1
    assert delivered_b == []


@pytest.mark.asyncio
async def test_max_images_flushes_immediately() -> None:
    agg = MatrixMediaBatchAggregator()
    delivered: list[tuple[list, str]] = []
    deliver = lambda b, t: delivered.append((b, t))  # noqa: E731
    for i in range(media_batch.MAX_BATCH_IMAGES):
        agg.add("!r:x", blocks=[_img(str(i))], caption="", deliver=deliver)
    assert len(delivered) == 1
    assert len(delivered[0][0]) == media_batch.MAX_BATCH_IMAGES


@pytest.mark.asyncio
async def test_deliver_exception_does_not_raise() -> None:
    agg = MatrixMediaBatchAggregator()

    def _boom(blocks, text):
        raise RuntimeError("deliver failed")

    agg.add("!r:x", blocks=[_img("1")], caption="", deliver=_boom)
    agg.flush_now("!r:x")  # 不抛
    assert "!r:x" not in agg._batches
