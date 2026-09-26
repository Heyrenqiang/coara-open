"""Composer image binding: late paste stays with the submit that was already waiting."""

from __future__ import annotations

import pytest

from src.cli import image_paste
from src.ui.attach_ws import AttachWsHandlers


def _reset() -> None:
    image_paste.discard_unsent_images()
    # discard bumps the epoch; tests that begin_paste compare against the new epoch.
    while image_paste._INFLIGHT:
        image_paste.finish_paste()
    image_paste._loading_event().set()


def _stage(name: str, *, in_buffer: bool) -> None:
    image_paste._remember_image({"type": "image", "source": name}, name)
    if in_buffer:
        image_paste.ack_latest_marker()


def test_unacked_image_survives_submit_snapshot_without_marker() -> None:
    _reset()
    _stage("图片1", in_buffer=False)
    assert image_paste.sync_cancel_if_marker_missing("先打的字") is False
    assert image_paste.pending_names() == ["图片1"]


def test_acked_image_drops_when_marker_was_deleted() -> None:
    _reset()
    _stage("图片1", in_buffer=True)
    assert image_paste.sync_cancel_if_marker_missing("先打的字") is True
    assert image_paste.pending_names() == []


def test_strip_markers_keeps_unrelated_brackets_when_no_images() -> None:
    assert image_paste.strip_markers("记得 [重要] 这件事", []) == "记得 [重要] 这件事"


def test_second_paste_keeps_loading_until_both_finish() -> None:
    _reset()
    image_paste.begin_paste()
    image_paste.begin_paste()
    try:
        image_paste.finish_paste()
        assert image_paste.is_image_loading() is True
    finally:
        image_paste.finish_paste()
    assert image_paste.is_image_loading() is False


def test_discard_invalidates_inflight_paste_epoch() -> None:
    _reset()
    epoch = image_paste.begin_paste()
    try:
        image_paste.discard_unsent_images()
        assert image_paste.composer_epoch() != epoch
    finally:
        image_paste.finish_paste()


@pytest.mark.asyncio
async def test_resolve_keeps_image_pasted_after_text_snapshot() -> None:
    _reset()
    _stage("图片1", in_buffer=False)
    text, images, paths = await image_paste.resolve_composer_attachments("先打的字")
    assert text == "先打的字"
    assert len(images) == 1
    assert paths == []
    assert image_paste.pending_names() == []


def test_cancel_image_only_followup_by_client_msg_id() -> None:
    from src.core.types import ContinuationInput

    image = ContinuationInput(text="", image_blocks=[{"type": "image"}], client_msg_id="img-1")
    system = ContinuationInput(text="<系统消息>\n后台\n</系统消息>", client_msg_id="")
    coara = type("C", (), {"_continuation_inputs": [image, system], "_continuation_event": None})()
    ok = AttachWsHandlers._cancel_last_continuation(coara, text="", client_msg_id="img-1")
    assert ok is True
    assert coara._continuation_inputs == [system]
