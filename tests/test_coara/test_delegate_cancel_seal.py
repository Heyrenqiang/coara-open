"""子智能体硬取消封存历史闭合（P0）：Ctrl+C 打断后封存的 record 不得带悬空 tool_call。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.types import Message, MessageRole, ToolCall
from src.tools.builtin.delegate import delegate as delegate_mod
from src.tools.builtin.delegate.delegate import DelegateToolInvocation
from tests.helpers import make_test_coara


@pytest.fixture(autouse=True)
def _reset_delegate_registries():
    delegate_mod._ACTIVE_SUBAGENTS.clear()
    delegate_mod._RUNNING_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()
    yield
    delegate_mod._ACTIVE_SUBAGENTS.clear()
    delegate_mod._RUNNING_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()


@pytest.mark.asyncio
async def test_cancelled_subagent_history_sealed_closed(tmp_path: Path) -> None:
    """运行中被 cancel 的子智能体：封存 record 的历史已闭合（无悬空 tool_call），resume 不再 400。"""
    parent = make_test_coara(tmp_path)
    inv = DelegateToolInvocation(
        {
            "action": "spawn",
            "description": "cancel seal test",
            "prompt": "do work",
            "subagent_type": "coaras",
        },
        parent,
    )

    subagent = make_test_coara(tmp_path / "sub")
    # 模拟被打断时的脏历史：user + 悬空 tool_call（无对应 tool_result）
    subagent.message_history = [
        Message(role=MessageRole.USER, content="任务"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="tc-1", name="read", arguments={"path": "x"})],
        ),
    ]

    async def _cancel_soon(*args, **kwargs):
        await asyncio.sleep(0)
        raise asyncio.CancelledError()
        yield  # noqa: unreachable — 让 process_message 是异步生成器（真实签名）

    subagent.process_message = _cancel_soon  # type: ignore[method-assign]
    # 封存路径需要 _emit_subagent_lifecycle_event / shutdown 可用（真实 CoaraBase 均有）

    store_dir = tmp_path / "sub"
    store = inv._subagent_store_for_dir(store_dir)
    subagent_id = "sa-test-cancelseal"

    result = await inv._run_subagent(subagent, subagent_id, subagent_config=None, tool_whitelist=set())

    assert result.is_cancelled or "取消" in (result.content or "")
    record = store.load(subagent_id)
    assert record is not None
    sealed = record.message_history
    # 悬空的 assistant tool_call 必须已被摘掉（sanitize_dangling_tool_tail）
    for entry in sealed:
        assert not (
            entry.get("role") == "assistant" and entry.get("tool_calls")
        ), f"sealed history still has dangling tool_calls: {entry}"
    # 也不得有孤儿 tool_result
    call_ids = {
        tc["id"]
        for entry in sealed
        for tc in (entry.get("tool_calls") or [])
    }
    for entry in sealed:
        if entry.get("role") == "tool" or entry.get("tool_call_id"):
            assert entry.get("tool_call_id") in call_ids
