"""delegate(action="wait") 与出口软提醒/放行语义。

模型：结果会合不再由框架硬等保证——LLM 显式调 wait 收结果；回合出口对
在跑的前台子智能体只提醒一次，提醒后仍不等则放行（released），迟到结果
按「忙则注入、闲则驻历史」路由。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.tool_base import ToolResult
from src.llm.provider import LLMResponse
from src.tools.builtin.delegate.delegate import DelegateToolInvocation
from tests.helpers import FakeProvider, make_test_coara


def _register(parent, task_id: str, task: asyncio.Task, description: str = "child") -> None:
    """注册前台子智能体并挂上与 spawn 路径一致的 done 回调。"""
    parent.register_foreground_delegate(task_id, task, description)
    task.add_done_callback(lambda t, tid=task_id, d=description: parent.on_foreground_delegate_done(tid, d, t))


async def _quick_result(text: str = "干完了") -> ToolResult:
    await asyncio.sleep(0.01)
    return ToolResult.success(content=text)


# ---------------------------------------------------------------------------
# delegate(action="wait")
# ---------------------------------------------------------------------------


def test_wait_requires_no_params() -> None:
    inv = DelegateToolInvocation({"action": "wait"}, None)
    assert inv.action == "wait"


def test_wait_without_pending_returns_immediately(tmp_path: Path) -> None:
    parent = make_test_coara(tmp_path)
    inv = DelegateToolInvocation({"action": "wait"}, parent)
    result = asyncio.run(inv._execute_wait())
    assert not result.is_error
    assert "没有在跑" in result.content


def test_wait_unknown_task_id_errors(tmp_path: Path) -> None:
    parent = make_test_coara(tmp_path)
    inv = DelegateToolInvocation({"action": "wait", "task_id": "sa-coaras-nope"}, parent)
    result = asyncio.run(inv._execute_wait())
    assert result.is_error
    assert "不在在跑" in result.content


def test_wait_returns_completed_roster_and_result_in_queue(tmp_path: Path) -> None:
    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        task = asyncio.create_task(_quick_result("页面做好了"))
        _register(parent, "sa-coaras-a", task, "做页面")

        inv = DelegateToolInvocation({"action": "wait"}, parent)
        result = await inv._execute_wait()
        assert not result.is_error
        assert "sa-coaras-a" in result.content
        assert "已完成" in result.content
        # 结果内容由 done 回调经接续队列送达（单一交付通道，无双份）
        assert any("页面做好了" in ci.text for ci in parent._continuation_inputs)

    asyncio.run(_run())


def test_wait_roster_covers_simultaneous_completions(tmp_path: Path) -> None:
    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        t1 = asyncio.create_task(_quick_result("结果一"))
        t2 = asyncio.create_task(_quick_result("结果二"))
        _register(parent, "sa-coaras-1", t1, "任务一")
        _register(parent, "sa-coaras-2", t2, "任务二")

        inv = DelegateToolInvocation({"action": "wait"}, parent)
        result = await inv._execute_wait()
        # 两个都在挂起期间完成 → 花名册全列出
        assert "sa-coaras-1" in result.content
        assert "sa-coaras-2" in result.content

    asyncio.run(_run())


def test_wait_wakes_on_continuation_input(tmp_path: Path) -> None:
    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        task = asyncio.create_task(asyncio.sleep(60))
        _register(parent, "sa-slow", task, "慢任务")
        try:
            inv = DelegateToolInvocation({"action": "wait"}, parent)
            waiter = asyncio.create_task(inv._execute_wait())
            await asyncio.sleep(0.05)
            parent.submit_continuation_input("用户催促")
            result = await asyncio.wait_for(waiter, timeout=2.0)
            assert "仍在运行" in result.content
        finally:
            task.cancel()

    asyncio.run(_run())


def test_wait_returns_immediately_when_input_prequeued(tmp_path: Path) -> None:
    """竞态回归：输入在 drain 之后、wait 之前到达（事件已被 clear）时，
    wait 不得空等——视同被提前唤醒，立即走早退路径。"""

    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        task = asyncio.create_task(asyncio.sleep(60))
        _register(parent, "sa-slow", task, "慢任务")
        try:
            # 输入先于 wait 到达（相当于 LLM 生成期间用户敲了字）
            parent.submit_continuation_input("早到的输入")
            inv = DelegateToolInvocation({"action": "wait"}, parent)
            result = await asyncio.wait_for(inv._execute_wait(), timeout=2.0)
            assert "仍在运行" in result.content
        finally:
            task.cancel()

    asyncio.run(_run())


def test_wait_reports_cancelled(tmp_path: Path) -> None:
    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        task = asyncio.create_task(asyncio.sleep(60))
        parent.register_foreground_delegate("sa-victim", task, "将被停")
        # 不挂 done 回调（task stop 路径下回调同样会跑，但取消不注入）
        inv = DelegateToolInvocation({"action": "wait"}, parent)
        waiter = asyncio.create_task(inv._execute_wait())
        await asyncio.sleep(0.05)
        task.cancel()
        result = await asyncio.wait_for(waiter, timeout=2.0)
        assert "已取消" in result.content

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 放行（release）与迟到结果路由
# ---------------------------------------------------------------------------


def test_release_moves_running_and_routes_idle_to_history(tmp_path: Path) -> None:
    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        task = asyncio.create_task(_quick_result("迟到结果"))
        _register(parent, "sa-late", task, "迟到任务")

        released = parent.release_pending_foreground_delegates()
        assert released == ["sa-late"]
        assert "sa-late" in parent._released_foreground_delegates
        assert not parent.has_pending_foreground_delegates()

        await asyncio.sleep(0.1)  # 让任务完成、回调执行
        assert "sa-late" not in parent._released_foreground_delegates
        # 会话空闲 → 进驻历史而非接续队列
        assert any("迟到结果" in str(m.content) for m in parent.message_history)
        assert not any("迟到结果" in ci.text for ci in parent._continuation_inputs)

    asyncio.run(_run())


def test_release_busy_injects_continuation(tmp_path: Path) -> None:
    async def _run() -> None:
        parent = make_test_coara(tmp_path)
        parent.has_active_turn = lambda: True  # type: ignore[assignment]
        task = asyncio.create_task(_quick_result("忙时结果"))
        _register(parent, "sa-busy", task, "忙时任务")

        parent.release_pending_foreground_delegates()
        await asyncio.sleep(0.1)
        assert any("忙时结果" in ci.text for ci in parent._continuation_inputs)

    asyncio.run(_run())


def test_cancel_all_covers_released(tmp_path: Path) -> None:
    parent = make_test_coara(tmp_path)

    async def _run() -> None:
        task = asyncio.create_task(asyncio.sleep(60))
        parent.register_foreground_delegate("sa-rel", task, "放行任务")
        parent.release_pending_foreground_delegates()
        parent.cancel_all_pending_foreground_delegates()
        await asyncio.sleep(0)
        assert task.cancelled() or task.cancelling() > 0
        assert parent._released_foreground_delegates == {}

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 回合级：软提醒一次 → 再收尾则放行；提醒后 wait 则正常会合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_exit_soft_reminder_then_release(tmp_path: Path) -> None:
    provider = FakeProvider([LLMResponse(content="先派活"), LLMResponse(content="收尾")])
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    child = asyncio.create_task(asyncio.sleep(60))
    _register(coara, "sa-slow", child, "慢任务")
    try:
        chunks = [chunk async for chunk in coara.process_message("开始")]
        joined = "".join(chunks)
        assert "先派活" in joined
        assert "收尾" in joined
        # 提醒只注入一次，且回合正常结束（不再硬等）
        reminders = [m for m in coara.message_history if "还有前台子智能体在跑" in str(m.content)]
        assert len(reminders) == 1
        assert "sa-slow" in str(reminders[0].content)
        assert "sa-slow" in coara._released_foreground_delegates
    finally:
        child.cancel()
