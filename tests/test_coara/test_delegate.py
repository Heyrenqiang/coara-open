"""Root delegate and workflow tool wiring tests."""

import asyncio
from pathlib import Path

import pytest

from src.coara.background_agent import BackgroundAgentManager
from src.coara.base import CoaraBase
from src.core.tool_base import ToolResult
from src.core.types import CoaraStatus, Message, MessageRole, ToolCall
from src.tools.builtin.delegate.delegate import DelegateToolInvocation
from tests.helpers import make_test_coara
from tests.real_env_helpers import managed_initialized_root


async def _await_foreground_delegate_result(parent: CoaraBase, placeholder: ToolResult) -> ToolResult:
    """Await the real result of a foreground async delegate.

    Foreground delegates no longer block ``execute()``: the invocation returns a
    placeholder immediately and the subagent runs as a registered asyncio.Task on
    the parent. The genuine ``ToolResult`` (final response / lifecycle events /
    error mapping) is produced by that task.
    """
    task_id = placeholder.metadata["task_id"]
    task = parent._pending_foreground_delegates[task_id]
    return await task


def test_aide_delegate_respects_background_flag() -> None:
    """aide 与 coaras 同机制：不再强制后台，前台/后台由调用方决定。"""
    inv = DelegateToolInvocation(
        {
            "description": "review",
            "prompt": "check",
            "subagent_type": "aide",
            "background": False,
        },
        None,
    )
    assert inv.background is False
    inv_bg = DelegateToolInvocation(
        {
            "description": "review",
            "prompt": "check",
            "subagent_type": "aide",
            "background": True,
        },
        None,
    )
    assert inv_bg.background is True


@pytest.mark.asyncio
async def test_environment_seed_skipped_when_flag_off(tmp_path: Path) -> None:
    """daily 关闭环境种子注入后，首回合不注入任何环境上下文/工作空间概况。"""
    from src.coara.turn_loop.user_turn_injectors import UserTurnContext, inject_environment_seed

    coara = make_test_coara(tmp_path)
    coara.inject_environment_seed = False
    ctx = UserTurnContext(content="任务：日常整理")
    await inject_environment_seed(coara, ctx)
    assert not coara.message_history


async def test_environment_seed_injected_by_default(tmp_path: Path) -> None:
    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX
    from src.coara.turn_loop.user_turn_injectors import UserTurnContext, inject_environment_seed
    from src.utils.message_content import message_content_to_text

    coara = make_test_coara(tmp_path)
    ctx = UserTurnContext(content="hello")
    await inject_environment_seed(coara, ctx)
    assert any(ENV_CONTEXT_PREFIX in message_content_to_text(m.content) for m in coara.message_history)


def test_resolve_tool_whitelist_uses_yaml_when_present() -> None:
    inv = DelegateToolInvocation(
        {
            "description": "advisor task",
            "prompt": "check",
            "subagent_type": "aide",
        },
        None,
    )
    assert inv._resolve_tool_whitelist(["read", "grep"]) == {"read", "grep"}


def test_resolve_tool_whitelist_inherits_parent_bound_tools(tmp_path: Path) -> None:
    parent = make_test_coara(tmp_path)
    parent._tool_manager.set_whitelist({"read", "grep", "glob"})
    inv = DelegateToolInvocation(
        {
            "description": "inherit",
            "prompt": "check",
            "subagent_type": "coaras",
        },
        parent,
    )
    assert inv._resolve_tool_whitelist([]) == {"read", "grep", "glob"}


@pytest.mark.asyncio
async def test_delegate_rejects_janitor_spawn(tmp_path: Path) -> None:
    """janitor 已机制化为内核维护管道，delegate 即使 system_dispatch 也拒绝派发。"""
    parent = make_test_coara(tmp_path)
    inv = DelegateToolInvocation(
        {
            "description": "janitor 会话间维护",
            "prompt": "<系统提醒>任务信封</系统提醒>",
            "subagent_type": "janitor",
            "background": True,
            "system_dispatch": True,
        },
        parent,
    )
    result = await inv.execute()
    assert result.is_error
    assert "内核维护" in (result.content or "")


def test_resolve_tool_whitelist_plan_mode_strips_write_tools(tmp_path: Path) -> None:
    """plan_mode 是硬只读契约：子代理 is_plan_mode=False 不受 executor 的
    计划文件限制 必须在 delegate 白名单里剥离写工具。"""
    parent = make_test_coara(tmp_path)
    parent._tool_manager.set_whitelist({"read", "grep", "write", "edit", "shell", "plan_mode", "delegate"})
    parent._tool_manager.enter_plan_mode(tmp_path / "plan.md")
    inv = DelegateToolInvocation(
        {
            "description": "read-only scout",
            "prompt": "check",
            "subagent_type": "coaras",
        },
        parent,
    )
    resolved = inv._resolve_tool_whitelist([])
    # parent_bound 分支也未过滤 shell——plan mode 下必须一并剥离
    assert "write" not in resolved
    assert "edit" not in resolved
    assert "plan_mode" not in resolved
    assert "shell" not in resolved
    assert "read" in resolved
    assert "grep" in resolved


def test_resolve_tool_whitelist_plan_mode_unbound_parent(tmp_path: Path) -> None:
    """无绑定白名单的 plan_mode 父级（正常路径）：子代理继承可见集并剥离写工具。"""
    parent = make_test_coara(tmp_path)
    parent._tool_manager.enter_plan_mode(tmp_path / "plan.md")
    inv = DelegateToolInvocation(
        {
            "description": "read-only scout",
            "prompt": "check",
            "subagent_type": "coaras",
        },
        parent,
    )
    resolved = inv._resolve_tool_whitelist([])
    assert resolved  # 不为空（空集在下游表示不限制）
    assert "write" not in resolved
    assert "edit" not in resolved
    assert "shell" not in resolved
    assert "read" in resolved


def test_resolve_tool_whitelist_plan_mode_exempts_janitor_and_daily(tmp_path: Path) -> None:
    """plan_mode 下派发 janitor/daily 不剥写工具：它们是系统维护（写 record/ws.md），
    不是改用户代码。否则 janitor 想 record 却只剩 read/grep/glob，陷死循环退不出。"""
    for subagent_type in ("janitor", "daily"):
        parent = make_test_coara(tmp_path)
        parent._tool_manager.set_whitelist(
            {"read", "grep", "write", "edit", "shell", "record", "plan_mode", "delegate"}
        )
        parent._tool_manager.enter_plan_mode(tmp_path / "plan.md")
        inv = DelegateToolInvocation(
            {
                "description": "maintenance",
                "prompt": "tidy",
                "subagent_type": subagent_type,
            },
            parent,
        )
        resolved = inv._resolve_tool_whitelist([])
        # 维护子智能体跳过 plan_mode 只读裁剪，保留写工具
        assert "edit" in resolved, f"{subagent_type} 应保留 edit"
        assert "write" in resolved, f"{subagent_type} 应保留 write"
        assert "record" in resolved, f"{subagent_type} 应保留 record"


@pytest.mark.asyncio
async def test_root_system_prompt_has_no_memory_index(tmp_path: Path) -> None:
    async with managed_initialized_root(tmp_path) as root:
        prompt = root._build_system_prompt()
        assert "Memory index" not in prompt
        assert "# Memory Index" not in prompt


@pytest.mark.asyncio
async def test_root_has_workflow_tools(tmp_path: Path):
    """Verify Root directly owns delegate and todo tools; workflow 工具已并入 delegate。"""
    async with managed_initialized_root(tmp_path) as root:
        assert "delegate" in root._tool_manager.tools
        assert "todo" in root._tool_manager.tools
        assert "reminder" in root._tool_manager.tools
        assert "ws" in root._tool_manager.tools
        assert "plan_mode" in root._tool_manager.tools
        # workflow 工具已移除：草案管理/外部运行统一走 delegate（激活 workflow 技能后长出参数）
        assert "workflow" not in root._tool_manager.tools
        assert "flow" not in root._tool_manager.tools


@pytest.mark.asyncio
async def test_delegate_advisor_gets_workflow_tools(monkeypatch, tmp_path: Path):
    """Verify aide/coaras 子智能体均无独立 workflow 工具（已并入 delegate）。"""
    async with managed_initialized_root(tmp_path) as root:
        seen: dict[str, CoaraBase] = {}

        async def fake_process_message(self: CoaraBase, content: str, **kwargs):
            seen[self.identity.name] = self
            yield "ok"

        monkeypatch.setattr(CoaraBase, "process_message", fake_process_message)

        advisor_result = await DelegateToolInvocation(
            {
                "description": "advisor review",
                "prompt": "review plan",
                "subagent_type": "aide",
                # aide 不再默认后台（与 coaras 统一为「前台/后台由调用方决定」）：
                # 测后台 aide 须显式传 background=true。
                "background": True,
            },
            root,
        ).execute()

        assert advisor_result.is_error is False
        assert advisor_result.metadata.get("mode") == "background"
        task_id = advisor_result.metadata["task_id"]
        mgr = BackgroundAgentManager()
        deadline = asyncio.get_running_loop().time() + 5.0
        while mgr.is_running(task_id) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        advisor_coara = next(coara for name, coara in seen.items() if name.startswith("sa-aide-"))
        advisor_visible_tools = {tool["name"] for tool in advisor_coara._get_visible_tool_definitions()}

        assert advisor_result.is_error is False
        assert "workflow" not in advisor_coara._tool_manager.tools
        assert "workflow" not in advisor_visible_tools

        general_result = await DelegateToolInvocation(
            {
                "description": "general",
                "prompt": "do work",
                "subagent_type": "coaras",
            },
            root,
        ).execute()

        assert general_result.is_error is False
        # Foreground delegates run async: await the registered task so the
        # subagent's process_message has actually executed before inspecting.
        general_tool_result = await _await_foreground_delegate_result(root, general_result)
        assert general_tool_result.is_error is False

        general_coara = next(coara for name, coara in seen.items() if name.startswith("sa-coaras-"))
        general_visible_tools = {tool["name"] for tool in general_coara._get_visible_tool_definitions()}

        assert "workflow" not in general_coara._tool_manager.tools
        assert "workflow" not in general_visible_tools
        # coaras 是任务型子智能体：不静默，工具改动正常显示
        assert general_coara._cli_silent is False


@pytest.mark.asyncio
async def test_delegate_returns_only_subagent_final_response(monkeypatch, tmp_path: Path):
    """Verify delegate strips subagent progress output and returns only the final handoff."""
    async with managed_initialized_root(tmp_path) as root:

        async def fake_process_message(self: CoaraBase, content: str, **kwargs):
            self.message_history.append(Message(role=MessageRole.USER, content=content))
            self.message_history.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="tc-1", name="todo", arguments={"action": "clear"})],
                )
            )
            yield "\n> 🛠️ **调用工具**: `todo`\n"
            self.message_history.append(
                Message(role=MessageRole.TOOL_RESULT, content="todo updated", tool_call_id="tc-1")
            )
            self.message_history.append(Message(role=MessageRole.ASSISTANT, content="任务结果：最终交付"))
            yield "任务结果：最终交付"

        monkeypatch.setattr(CoaraBase, "process_message", fake_process_message)

        result = await DelegateToolInvocation(
            {
                "description": "delegate final response",
                "prompt": "finish the task",
                "subagent_type": "coaras",
            },
            root,
        ).execute()

        assert result.is_error is False
        assert result.metadata["mode"] == "foreground_async"
        # Foreground delegates return a placeholder; the real subagent result is
        # delivered by the registered task.
        final = await _await_foreground_delegate_result(root, result)
        assert final.is_error is False
        assert final.content == "任务结果：最终交付"
        assert "调用工具" not in final.content
        assert final.metadata["subagent_type"] == "coaras"
        assert final.metadata["child_session_id"]


@pytest.mark.asyncio
async def test_delegate_emits_subagent_lifecycle_events(monkeypatch, tmp_path: Path):
    """Verify delegate emits start/complete lifecycle events for CLI subscribers."""
    async with managed_initialized_root(tmp_path) as root:
        events: list[tuple[str, dict]] = []
        root.set_trace_sink(lambda event: events.append((event.event_type, event.payload)))

        async def fake_process_message(self: CoaraBase, content: str, **kwargs):
            self.message_history.append(Message(role=MessageRole.USER, content=content))
            self.message_history.append(Message(role=MessageRole.ASSISTANT, content="任务结果：最终交付"))
            yield "任务结果：最终交付"

        monkeypatch.setattr(CoaraBase, "process_message", fake_process_message)

        result = await DelegateToolInvocation(
            {
                "description": "delegate lifecycle",
                "prompt": "finish the task",
                "subagent_type": "coaras",
            },
            root,
        ).execute()

        assert result.is_error is False
        # subagent_complete is emitted when the async foreground task finishes.
        final = await _await_foreground_delegate_result(root, result)
        assert final.is_error is False
        event_types = [event_type for event_type, _payload in events]
        assert "subagent_start" in event_types
        assert "subagent_complete" in event_types

        start_payload = next(payload for event_type, payload in events if event_type == "subagent_start")
        complete_payload = next(payload for event_type, payload in events if event_type == "subagent_complete")
        assert start_payload["subagent_type"] == "coaras"
        assert start_payload["description"] == "delegate lifecycle"
        assert start_payload["subagent_id"].startswith("sa-coaras-")
        assert complete_payload["subagent_id"] == start_payload["subagent_id"]


@pytest.mark.asyncio
async def test_delegate_returns_error_when_subagent_fails(monkeypatch, tmp_path: Path):
    """Verify delegate maps subagent runtime failure to ToolResult.error."""
    async with managed_initialized_root(tmp_path) as root:

        async def fake_process_message(self: CoaraBase, content: str, **kwargs):
            self.message_history.append(Message(role=MessageRole.USER, content=content))
            self.status = CoaraStatus.FAILED
            yield "Error: 子代理失败"

        monkeypatch.setattr(CoaraBase, "process_message", fake_process_message)

        result = await DelegateToolInvocation(
            {
                "description": "delegate failure",
                "prompt": "trigger failure",
                "subagent_type": "coaras",
            },
            root,
        ).execute()

        assert result.is_error is False
        # Foreground delegates return a placeholder; the subagent failure
        # surfaces as ToolResult.error on the registered task.
        final = await _await_foreground_delegate_result(root, result)
        assert final.is_error is True
        # 判死文案 = 原始错误 + 失败分类附注（本用例无 _turn_failure，
        # 分类器按文案判为「确定性失败」→ 附 resume 无意义说明）。
        assert final.content.startswith("Error: 子代理失败")
        assert "确定性失败" in final.content


@pytest.mark.asyncio
async def test_delegate_research_returns_skill_hint(tmp_path: Path) -> None:
    async with managed_initialized_root(tmp_path) as root:
        result = await DelegateToolInvocation(
            {
                "description": "调研",
                "prompt": "查资料",
                "subagent_type": "research",
            },
            root,
        ).execute()
        assert result.is_error is True
        assert "skill" in result.content.lower() or "research" in result.content
        assert "research" in result.content


@pytest.mark.asyncio
async def test_delegate_explore_returns_coaras_hint(tmp_path: Path) -> None:
    async with managed_initialized_root(tmp_path) as root:
        result = await DelegateToolInvocation(
            {
                "description": "摸底",
                "prompt": "只读查结构",
                "subagent_type": "explore",
            },
            root,
        ).execute()
        assert result.is_error is True
        assert "explore" in result.content
        assert "coaras" in result.content
