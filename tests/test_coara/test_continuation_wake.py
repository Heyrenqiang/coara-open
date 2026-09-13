"""接续输入唤醒语义（agentic wait 模型）。

会合机制已从「框架在回合出口硬等」改为「LLM 显式 delegate(action="wait")」，
唤醒点随之迁移：等待发生在 wait 工具内部，用户接续输入经
`_continuation_event` 提前唤醒 wait（覆盖见 test_delegate_wait.py::
test_wait_wakes_on_continuation_input）。本文件保留事件本身的单元语义，
并锁定「软提醒回合里用户输入不丢」的回合级行为。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.llm.provider import LLMResponse
from tests.helpers import FakeProvider, make_test_coara


@pytest.mark.asyncio
async def test_continuation_event_set_and_cleared(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    assert not coara._continuation_event.is_set()
    coara.submit_continuation_input("a")
    assert coara._continuation_event.is_set()
    assert [ci.text for ci in coara.drain_continuation_inputs()] == ["a"]
    assert not coara._continuation_event.is_set()


@pytest.mark.asyncio
async def test_continuation_input_during_soft_reminder_turn_is_processed(tmp_path: Path) -> None:
    """软提醒回合：用户在提醒后的迭代间接续输入，必须进历史并被 LLM 看到。"""
    provider = FakeProvider(
        [
            LLMResponse(content="先派活"),
            LLMResponse(content="看到催促"),
            LLMResponse(content="收尾"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    child = asyncio.create_task(asyncio.sleep(60))
    coara.register_foreground_delegate("sa-slow", child, "慢任务")

    original_complete = provider.complete
    call_count = 0

    async def _complete_with_input(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # 第二次 LLM 调用（软提醒之后）期间，用户接续输入到达
            coara.submit_continuation_input("用户催促")
        return await original_complete(*args, **kwargs)

    provider.complete = _complete_with_input  # type: ignore[method-assign]

    try:
        chunks = [chunk async for chunk in coara.process_message("开始")]
        joined = "".join(chunks)
        assert "看到催促" in joined
        history_text = "\n".join(str(m.content) for m in coara.message_history)
        assert "用户催促" in history_text
        # 输入到达后仍有未完成的子智能体且已提醒过 → 放行，回合结束
        assert "sa-slow" in coara._released_foreground_delegates
    finally:
        child.cancel()


@pytest.mark.asyncio
async def test_continuation_image_injected_as_multimodal(tmp_path: Path) -> None:
    """Plan B：带图接续输入以多模态 user 消息注入，图片不退化文字。"""
    from src.core.types import MessageRole

    provider = FakeProvider([LLMResponse(content="收到图片")])
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    img = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
    }
    coara.submit_continuation_input("看这张图", image_blocks=[img])

    chunks = [chunk async for chunk in coara.process_message("继续")]

    multimodal = [
        m
        for m in coara.message_history
        if m.role == MessageRole.USER
        and isinstance(m.content, list)
        and any(b.get("type") == "image" for b in m.content)
    ]
    assert multimodal, "应存在一条多模态 user 消息"
    content = multimodal[0].content
    assert any(b.get("type") == "text" and "看这张图" in b["text"] for b in content)
    # 纯文本跟话仍走原链路（不带图）
    assert "".join(chunks) == "收到图片"


def test_continuation_followup_display_line_image_only() -> None:
    from src.core.message_tags import continuation_followup_display_line

    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
    assert continuation_followup_display_line("", image_blocks=[img]) == "[图片]"
    assert continuation_followup_display_line("", image_blocks=[img, img]) == "[图片×2]"
    assert continuation_followup_display_line("看这张图", image_blocks=[img]) == "看这张图"


@pytest.mark.asyncio
async def test_root_coara_continuation_proxy(tmp_path: Path) -> None:
    """RootCoara 门面与 CoaraBase 一致：带图提交与 ContinuationInput 出队。"""
    from src.coara.root import RootCoara

    coara = make_test_coara(tmp_path)
    root = RootCoara.__new__(RootCoara)
    root._foreground_session_id = "ws-test"
    root._sessions = {root._foreground_session_id: SimpleNamespace(coara=coara)}
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
    root.submit_continuation_input("跟话", image_blocks=[img])
    drained = root.drain_continuation_inputs()
    assert len(drained) == 1
    assert drained[0].text == "跟话"
    assert drained[0].image_blocks == [img]


def test_pending_input_hint_shows_image_only_followup(tmp_path: Path) -> None:
    from src.cli.input_queue_display import pending_input_hint_lines

    coara = make_test_coara(tmp_path)
    coara.has_active_turn = lambda: True  # type: ignore[method-assign, assignment]
    img = {"type": "image"}
    coara.submit_continuation_input("", image_blocks=[img])
    lines = pending_input_hint_lines(SimpleNamespace(foreground_coara=coara))
    assert lines == ["→ [图片]"]


def test_submit_continuation_input_records_source(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    coara.submit_continuation_input("手机跟话", source="matrix")
    items = coara.drain_continuation_inputs()
    assert len(items) == 1
    assert items[0].text == "手机跟话"
    assert items[0].source == "matrix"


@pytest.mark.asyncio
async def test_matrix_continuation_during_background_turn_keeps_matrix_source(tmp_path: Path) -> None:
    """手机跟话注入 background 唤醒回合时，user_message 必须带 source=matrix。

    旧逻辑继承 ``_active_turn_source=background``，CLI 会把「成功了吗」打成
    ``[后台]：`` 再叠一层「你：」，双显。
    """
    provider = FakeProvider(
        [
            LLMResponse(content="先处理后台结果"),
            LLMResponse(content="看到手机催促"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    user_message_payloads: list[dict] = []
    original_emit = coara._emit_trace

    def _capture(event_type: str, message: str = "", **kwargs):
        if event_type == "user_message":
            user_message_payloads.append(dict(kwargs.get("payload") or {}))
        return original_emit(event_type, message, **kwargs)

    coara._emit_trace = _capture  # type: ignore[method-assign]

    original_complete = provider.complete
    call_count = 0

    async def _complete_with_phone(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            coara.submit_continuation_input("成功了吗", source="matrix")
        return await original_complete(*args, **kwargs)

    provider.complete = _complete_with_phone  # type: ignore[method-assign]

    chunks = [chunk async for chunk in coara.process_message("后台任务完成", source="background")]
    assert "看到手机催促" in "".join(chunks)

    cont = [p for p in user_message_payloads if p.get("continuation")]
    assert cont, "应发出接续 user_message"
    assert cont[0].get("source") == "matrix"
    assert cont[0].get("content") == "成功了吗"
    assert all(p.get("source") != "background" for p in cont)
