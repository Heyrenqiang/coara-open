"""执行器层审批门 fail-closed 回归（审批静默放行根修）。

回归场景（09-08 深夜三次实验复现）：daemon/tray 内核恒无 tty，手机端
发起带审批标记的工具调用时，若审批请求没有任何可送达通道（无 CLI modal、
无远端回合通道、无常驻 Matrix 审批通道），工具**绝不执行**。

- 无任何通道：write + require_approval=True 不落盘，结果为错误且注明未执行
- 有远端回合通道（Matrix/Web 在线）：审批照常送达，用户同意后执行
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent.executor import ToolExecutor
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import ToolCall
from tests.helpers import make_test_coara


class _WriteInvocation(ToolInvocation):
    """真实落盘的写工具 invocation（断言「未被审批不落盘」用）。"""

    def get_description(self) -> str:
        return f"Write: {self.params.get('path')}"

    async def execute(self, signal=None) -> ToolResult:
        path = Path(str(self.params["path"]))
        path.write_text(str(self.params.get("contents", "")), encoding="utf-8")
        return ToolResult.success(f"written: {path}")


class _ApprovalGatedWriteTool(BaseTool):
    """声明恒需审批的写工具（等价于 LLM require_approval=true 的场景）。"""

    name = "gated_write"
    description = "write tool that always requires approval"
    kind = ToolKind.OTHER

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:  # noqa: ARG004
        return True

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _WriteInvocation(params)


def _no_channel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 daemon：无 CLI modal、无回合通道（审批只发回合来源端，无 standing 逃生门）。"""
    from src.coara.frontend import get_frontend

    async def _no_cli(**_kwargs: Any) -> Any:
        raise RuntimeError("CLI prompt not registered")

    monkeypatch.setattr(get_frontend(), "prompt_select", _no_cli)
    monkeypatch.setattr("src.coara.turn_context.get_end_channel", lambda: None)
    monkeypatch.setattr("src.coara.turn_context.get_turn_channel", lambda: None)


@pytest.mark.asyncio
async def test_approval_gated_tool_not_executed_without_any_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """外部人员（untrusted）回合：无 tty 且无任何审批通道时 fail-closed，不落盘。"""
    _no_channel_env(monkeypatch)

    coara = make_test_coara(tmp_path)
    coara.register_tool(_ApprovalGatedWriteTool())
    await coara.initialize()
    # 对外身份才走工具级硬门；拥有者本人的回合不做硬门（见 owner 两条用例）
    object.__setattr__(coara, "_current_trust_level", "untrusted")

    target = tmp_path / "should_not_exist.txt"
    executor = ToolExecutor()
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-1", name="gated_write", arguments={"path": str(target), "contents": "PWN"})],
        is_owner=True,
    )

    result = executions[0].result
    assert result.is_error, f"expected blocked, got: {result.content!r}"
    assert "审批" in str(result.content) or "无法送达" in str(result.content)
    assert not target.exists(), "审批未通过却落盘——静默放行复现"


@pytest.mark.asyncio
async def test_approval_gated_tool_executes_after_remote_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远端回合通道在线时审批正常送达：用户同意后执行并落盘。"""
    _no_channel_env(monkeypatch)

    asked: list[str] = []

    class _FakeRemoteChannel:
        async def send_text(self, body: str) -> bool:  # noqa: ARG002
            return True

        async def confirm(self, question, options, *, timeout_seconds=300.0, signal=None):  # noqa: ARG002
            asked.append(question)
            return True  # 用户点「同意」

    monkeypatch.setattr(
        "src.coara.turn_context.get_turn_channel",
        lambda: _FakeRemoteChannel(),
    )

    coara = make_test_coara(tmp_path)
    coara.register_tool(_ApprovalGatedWriteTool())
    await coara.initialize()
    object.__setattr__(coara, "_current_trust_level", "untrusted")

    target = tmp_path / "approved.txt"
    executor = ToolExecutor()
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-2", name="gated_write", arguments={"path": str(target), "contents": "OK"})],
        is_owner=True,
    )

    result = executions[0].result
    assert not result.is_error, f"expected executed after approval, got: {result.content!r}"
    assert asked, "审批请求未送达远端通道"
    assert target.read_text(encoding="utf-8") == "OK"


@pytest.mark.asyncio
async def test_owner_context_bypasses_tool_hard_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """拥有者本人的回合：工具级静态硬门不参与，声明恒需审批的工具也直接执行。

    审批只服务两件事——模型自己声明（require_approval）与对外加严（untrusted）。
    内部不再由命令名/路径的静态名单替模型判断什么危险。
    """
    _no_channel_env(monkeypatch)

    coara = make_test_coara(tmp_path)
    coara.register_tool(_ApprovalGatedWriteTool())
    await coara.initialize()

    target = tmp_path / "owner_ok.txt"
    executor = ToolExecutor()
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-3", name="gated_write", arguments={"path": str(target), "contents": "OK"})],
        is_owner=True,
    )

    result = executions[0].result
    assert not result.is_error, f"owner 回合不该被静态硬门拦：{result.content!r}"
    assert target.read_text(encoding="utf-8") == "OK"


@pytest.mark.asyncio
async def test_llm_declared_approval_still_prompts_for_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型自己声明要审批时拥有者回合照弹：放权不等于取消模型自审。"""
    _no_channel_env(monkeypatch)

    coara = make_test_coara(tmp_path)
    coara.register_tool(_ApprovalGatedWriteTool())
    await coara.initialize()

    target = tmp_path / "llm_gated.txt"
    executor = ToolExecutor()
    executions = await executor.execute(
        coara,
        [
            ToolCall(
                id="call-4",
                name="gated_write",
                arguments={"path": str(target), "contents": "NO", "require_approval": True},
            )
        ],
        is_owner=True,
    )

    result = executions[0].result
    assert result.is_error, "无通道时模型声明的审批应 fail-closed"
    assert not target.exists()
