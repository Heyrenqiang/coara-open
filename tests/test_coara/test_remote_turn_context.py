"""mid-turn 远端接续输入恢复 turn 上下文的回归测试。

背景：Matrix ingress 在回合忙时把消息 defer 进接续队列
（``try_defer_to_continuation_input``），该路径不经过 ``turn``。
修复前消费接续输入时 ``get_turn_channel()`` 为 None，审批/询问/
plan_review 退化为本地 modal 或非交互自动放行，手机端收不到审批卡片。
修复：defer 时记录远端上下文，turn loop 消费远端输入时恢复 ContextVar，
回合收尾统一清理。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.coara.turn_context import (
    get_turn_channel,
    get_turn_channel_id,
    get_turn_send_text,
    reset_turn_context,
    set_turn_context,
)
from src.core.types import CoaraStatus
from src.llm.provider import LLMResponse
from src.matrix_client.ingress_helpers import try_defer_to_continuation_input
from tests.helpers import FakeProvider, make_test_coara


def test_set_reset_turn_context_roundtrip() -> None:
    """set_turn_context 恢复 ContextVar，reset 后回到初始空值。"""
    assert get_turn_channel_id() is None
    assert get_turn_channel() is None
    assert get_turn_send_text() is None

    channel = SimpleNamespace()
    tokens = set_turn_context("!room:test", send_text=lambda _r, _b: True, interaction_channel=channel)
    try:
        assert get_turn_channel_id() == "!room:test"
        assert get_turn_channel() is channel
        assert get_turn_send_text() is not None
    finally:
        reset_turn_context(tokens)

    assert get_turn_channel_id() is None
    assert get_turn_channel() is None
    assert get_turn_send_text() is None


@pytest.mark.asyncio
async def test_defer_records_remote_ctx_on_matrix_channel(tmp_path) -> None:
    """Matrix defer 路径记录远端上下文并提交带 source=matrix 的裸输入。"""
    coara = make_test_coara(tmp_path)
    coara._active_turn = object()
    coara.status = CoaraStatus.RUNNING
    root = SimpleNamespace(foreground_coara=coara)
    channel = SimpleNamespace()

    handled = try_defer_to_continuation_input(
        root,
        "继续",
        channel="matrix",
        room_id="!room:test",
        send_text=lambda _r, _b: True,
        interaction_channel=channel,
    )

    assert handled is True
    assert coara._continuation_inputs
    item = coara._continuation_inputs[0]
    assert item.deferred_remote_ctx is not None
    room_id, send_text, interaction_channel, source = item.deferred_remote_ctx[:4]
    assert room_id == "!room:test"
    assert send_text is not None
    assert interaction_channel is channel
    assert source == "matrix"
    # Stamp moved onto the queue item; shared slot must not linger for a later end.
    assert coara._deferred_remote_ctx is None
    # 输入存裸文本，来源标签在注入 message_history 时由内核按 source 现包
    assert item.text.strip() == "继续"
    assert item.source == "matrix"


@pytest.mark.asyncio
async def test_defer_skips_ctx_when_no_room(tmp_path) -> None:
    """非 Matrix defer 或缺少 room/send_text 时不记录远端上下文。"""
    coara = make_test_coara(tmp_path)
    coara._active_turn = object()
    coara.status = CoaraStatus.RUNNING
    root = SimpleNamespace(foreground_coara=coara)

    handled = try_defer_to_continuation_input(root, "继续", channel="matrix")

    assert handled is True
    assert coara._deferred_remote_ctx is None


@pytest.mark.asyncio
async def test_turn_loop_restores_remote_context_for_remote_continuation(tmp_path) -> None:
    """消费远端接续输入时 turn loop 恢复 turn 上下文，回合收尾清理。"""
    seen: dict[str, object] = {}

    class ProbeProvider(FakeProvider):
        async def complete(self, *args, **kwargs):
            seen["room_id"] = get_turn_channel_id()
            seen["channel"] = get_turn_channel()
            return await super().complete(*args, **kwargs)

    provider = ProbeProvider([LLMResponse(content="好的")])
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    channel = SimpleNamespace()
    coara.set_deferred_remote_ctx("!room:test", lambda _r, _b: True, channel)
    # source=matrix 裸文本：turn loop 按 source 判定恢复 turn（不再看内容标签）
    coara.submit_continuation_input("继续处理", source="matrix")

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert seen.get("room_id") == "!room:test"
    assert seen.get("channel") is channel
    assert chunks
    # 回合结束后远端上下文已清理，不泄漏到下一回合
    assert get_turn_channel_id() is None
    assert get_turn_channel() is None
    assert coara._turn_context_tokens == []
    assert coara._deferred_remote_ctx is None


def test_deferred_remote_ctx_is_per_continuation_item(tmp_path) -> None:
    """同回合两端先后跟话：后写不得覆盖先写的审批通道。"""
    coara = make_test_coara(tmp_path)
    web_ch = SimpleNamespace(name="web")
    mx_ch = SimpleNamespace(name="matrix")

    coara.set_deferred_remote_ctx("web-conn", lambda *_a: None, web_ch, source="web", actor="w1")
    coara.submit_continuation_input("web跟话", source="web")
    coara.set_deferred_remote_ctx("!room:mx", lambda *_a: None, mx_ch, source="matrix", actor="@u:mx")
    coara.submit_continuation_input("手机跟话", source="matrix")

    assert len(coara._continuation_inputs) == 2
    web_item, mx_item = coara._continuation_inputs
    assert web_item.deferred_remote_ctx is not None
    assert mx_item.deferred_remote_ctx is not None
    assert web_item.deferred_remote_ctx[0] == "web-conn"
    assert web_item.deferred_remote_ctx[2] is web_ch
    assert mx_item.deferred_remote_ctx[0] == "!room:mx"
    assert mx_item.deferred_remote_ctx[2] is mx_ch
    assert coara._deferred_remote_ctx is None
    assert coara._pending_deferred_remote_ctx is None
