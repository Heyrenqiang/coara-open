"""Tests for plan mode tool and visibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara.turn_completion import PlanSubmittedError
from src.tools.builtin.planning.plan_mode import PlanModeTool


@pytest.mark.asyncio
async def test_exit_plan_mode_restores_tools_without_submitting_plan(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Plan\n", encoding="utf-8")

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file
        exited = False

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False
            self.exited = True

    parent = _Parent()
    tool = PlanModeTool(parent_coara=parent)

    result = await tool.create_invocation({"action": "exit"}).execute()

    assert not result.is_error
    assert parent.exited is True
    assert parent.is_plan_mode is False
    assert result.metadata == {"plan_file": str(plan_file), "cancelled": True}


@pytest.mark.asyncio
async def test_submit_plan_publishes_review_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## 方案\n- 步骤一", encoding="utf-8")

    sent: list[str] = []

    class _RemoteChannel:
        async def send_text(self, body: str) -> bool:
            sent.append(body)
            return True

        async def prompt_select(self, *args: object, **kwargs: object):
            raise AssertionError("对话式审批不应再弹卡片")

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file
        exited = False

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False
            self.exited = True

    parent = _Parent()
    monkeypatch.setattr(
        "src.coara.turn_context.get_turn_channel",
        lambda: _RemoteChannel(),
    )
    tool = PlanModeTool(parent_coara=parent)

    with pytest.raises(PlanSubmittedError) as exc_info:
        await tool.create_invocation({"action": "submit", "content": "## 方案\n- 步骤一"}).execute()

    assert exc_info.value.plan_file == str(plan_file)
    assert parent.exited is False  # 不立即退出，等待用户回复
    assert len(sent) == 1
    assert "## 方案" in sent[0]
    assert "请审阅" in sent[0]
    # 无预设选项：用户自由回复
    assert "1. 批准" not in sent[0]


@pytest.mark.asyncio
async def test_submit_plan_delivers_via_end_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端通道优先：parent 有 _deliver_plan_review_to_end（attach/web 回合）时走正文
    chunk 通路投递，不再依赖 remote send_text 的 info 帧。"""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## 方案\n- 步骤一", encoding="utf-8")

    delivered: list[str] = []

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file
        exited = False
        _plan_pending_approval = False

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False
            self.exited = True

        async def _deliver_plan_review_to_end(self, body: str) -> bool:
            delivered.append(body)
            return True

    def _no_remote() -> object:
        raise AssertionError("端通道投递成功时不应再走 remote send_text")

    monkeypatch.setattr("src.coara.turn_context.get_turn_channel", _no_remote)
    tool = PlanModeTool(parent_coara=_Parent())

    with pytest.raises(PlanSubmittedError):
        await tool.create_invocation({"action": "submit", "content": "## 方案\n- 步骤一"}).execute()

    assert len(delivered) == 1
    assert "## 方案" in delivered[0]
    assert "请审阅" in delivered[0]


@pytest.mark.asyncio
async def test_submit_plan_send_failure_does_not_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Plan\n", encoding="utf-8")

    class _RemoteChannel:
        async def send_text(self, body: str) -> bool:
            return False

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False

    monkeypatch.setattr(
        "src.coara.turn_context.get_turn_channel",
        lambda: _RemoteChannel(),
    )
    tool = PlanModeTool(parent_coara=_Parent())
    result = await tool.create_invocation({"action": "submit", "content": "## Plan\n"}).execute()
    assert result.is_error
    assert "未能发送" in (result.content or "")


@pytest.mark.asyncio
async def test_submit_plan_requires_content(tmp_path: Path) -> None:
    class _Parent:
        is_plan_mode = True
        plan_file_path = tmp_path / "plan.md"
        exited = False

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False
            self.exited = True

    parent = _Parent()
    tool = PlanModeTool(parent_coara=parent)

    result = await tool.create_invocation({"action": "submit"}).execute()

    assert result.is_error
    assert "必须带 content" in result.content
    assert parent.exited is False


@pytest.mark.asyncio
async def test_exit_blocked_while_pending_approval(tmp_path: Path) -> None:
    """待批准锁：submit 后同回合 exit 抛 CoaraRunCancelledError 收尾回合（拒绝即退出）；
    用户消息清锁后才放行。"""
    from src.coara.turn_completion import CoaraRunCancelledError

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Plan\n", encoding="utf-8")

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file
        exited = False
        _plan_pending_approval = True  # submit 后由工具置位

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False
            self.exited = True

    parent = _Parent()
    tool = PlanModeTool(parent_coara=parent)

    with pytest.raises(CoaraRunCancelledError):
        await tool.create_invocation({"action": "exit"}).execute()
    assert parent.exited is False
    assert parent.is_plan_mode is True

    # 用户下一条消息到达（base 清锁）后 exit 恢复可用
    parent._plan_pending_approval = False
    result2 = await tool.create_invocation({"action": "exit"}).execute()
    assert not result2.is_error
    assert parent.exited is True
    assert parent.is_plan_mode is False


@pytest.mark.asyncio
async def test_resubmit_blocked_while_pending_approval(tmp_path: Path) -> None:
    """待批准锁：submit 后同回合重复 submit 抛 CoaraRunCancelledError 收尾（调整要等用户消息后重提）。"""
    from src.coara.turn_completion import CoaraRunCancelledError

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Plan\n", encoding="utf-8")

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file
        _plan_pending_approval = True

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False

    tool = PlanModeTool(parent_coara=_Parent())
    with pytest.raises(CoaraRunCancelledError):
        await tool.create_invocation({"action": "submit", "content": "## 方案\nv2"}).execute()


@pytest.mark.asyncio
async def test_submit_sets_pending_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """submit 成功后置待批准锁（base 在用户消息到达时清锁）。"""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Plan\n", encoding="utf-8")

    class _RemoteChannel:
        async def send_text(self, body: str) -> bool:
            return True

    class _Parent:
        is_plan_mode = True
        plan_file_path = plan_file
        _plan_pending_approval = False

        def exit_plan_mode(self) -> None:
            self.is_plan_mode = False

    monkeypatch.setattr(
        "src.coara.turn_context.get_turn_channel",
        lambda: _RemoteChannel(),
    )
    parent = _Parent()
    tool = PlanModeTool(parent_coara=parent)
    with pytest.raises(PlanSubmittedError):
        await tool.create_invocation({"action": "submit", "content": "## 方案"}).execute()
    assert parent._plan_pending_approval is True


@pytest.mark.asyncio
async def test_pending_approval_cleared_by_user_input(tmp_path: Path) -> None:
    """base 层清锁：带来源的接续输入（用户跟话）解除待批准；系统注入（无 source）不清。"""
    from src.llm.provider import LLMResponse
    from tests.helpers import FakeProvider, make_test_coara

    coara = make_test_coara(tmp_path, provider=FakeProvider([LLMResponse(content="ok")]))
    await coara.initialize()
    coara._plan_pending_approval = True

    coara.submit_continuation_input("开始吧", source="web")
    assert coara._plan_pending_approval is False

    coara._plan_pending_approval = True
    coara.submit_continuation_input("<系统消息>子代理已完成</系统消息>")
    assert coara._plan_pending_approval is True


@pytest.mark.asyncio
async def test_pending_approval_cleared_by_new_turn(tmp_path: Path) -> None:
    """base 层清锁：交互前端新回合（用户下一条消息）解除待批准。"""
    from src.llm.provider import LLMResponse
    from tests.helpers import FakeProvider, make_test_coara

    coara = make_test_coara(tmp_path, provider=FakeProvider([LLMResponse(content="ok")]))
    await coara.initialize()
    coara._plan_pending_approval = True

    _ = [chunk async for chunk in coara.process_message("开始", source="web")]
    assert coara._plan_pending_approval is False
