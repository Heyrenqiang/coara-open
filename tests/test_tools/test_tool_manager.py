"""Tests for runtime tool registration and tool manager visibility."""

from __future__ import annotations

from pathlib import Path

from src.coara.runtime_tools import register_runtime_tools
from src.coara.tool_manager import ToolManager
from src.tools.builtin.planning.plan_mode import PlanModeTool
from tests.helpers import make_test_coara


def test_subagent_depth_does_not_register_delegate(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path, name="sa-research-test")
    coara.delegate_depth = 1
    register_runtime_tools(coara)
    assert "delegate" not in coara._tool_manager.tools


def test_root_depth_registers_delegate(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path, name="root-test")
    coara.delegate_depth = 0
    register_runtime_tools(coara)
    assert "delegate" in coara._tool_manager.tools


def test_plan_mode_visible_while_in_plan_mode(tmp_path: Path) -> None:
    manager = ToolManager()
    manager.register_tool(PlanModeTool())
    manager.enter_plan_mode(tmp_path / "plan.md")

    visible_names = {definition["name"] for definition in manager.get_visible_tool_definitions(is_owner_ctx=True)}

    assert "plan_mode" in visible_names


def test_disabled_tool_hidden_from_definitions_and_names() -> None:
    manager = ToolManager()
    manager.register_tool(PlanModeTool())
    manager.set_disabled({"plan_mode"})

    names = manager.get_visible_tool_names(is_owner_ctx=True)
    definitions = manager.get_visible_tool_definitions(is_owner_ctx=True)
    assert "plan_mode" not in names
    assert "plan_mode" not in {d["name"] for d in definitions}
    assert manager.is_disabled("plan_mode")
    assert not manager.is_disabled("read")


def test_disabled_tool_cannot_be_revealed() -> None:
    from src.tools.builtin.scheduling.reminder import ReminderTool

    manager = ToolManager()
    manager.register_tool(ReminderTool())
    assert manager.reveal_tool("reminder") is True
    manager.set_disabled({"reminder"})
    assert manager.reveal_tool("reminder") is False
    assert "reminder" not in manager._revealed


def test_disabled_deferred_tool_hidden_from_summaries() -> None:
    from src.tools.builtin.scheduling.reminder import ReminderTool

    manager = ToolManager()
    manager.register_tool(ReminderTool())
    manager.set_disabled({"reminder"})
    summaries = manager.get_deferred_tool_summaries(is_owner_ctx=True)
    assert "reminder" not in {s["name"] for s in summaries}


def test_subagent_tool_schemas_omit_approval_meta(tmp_path: Path) -> None:
    """子智能体装配的 write/edit/delete/shell 不含 require_approval 元参数。"""
    from types import SimpleNamespace

    from src.tools.builtin.runtime.shell import ShellTool

    root_owner = SimpleNamespace(delegate_depth=0, identity=SimpleNamespace(user_facing=True, persona=None))
    root_mgr = ToolManager(owner=root_owner)
    root_mgr.register_tool(ShellTool(workspace_root=tmp_path))
    root_defs = {d["name"]: d for d in root_mgr.get_visible_tool_definitions(is_owner_ctx=True)}
    assert "require_approval" in root_defs["shell"]["parameters"]["properties"]
    assert "approval_reason" in root_defs["shell"]["parameters"]["properties"]

    sub_owner = SimpleNamespace(delegate_depth=1, identity=SimpleNamespace(user_facing=False, persona=None))
    sub_mgr = ToolManager(owner=sub_owner)
    sub_mgr.register_tool(ShellTool(workspace_root=tmp_path))
    sub_defs = {d["name"]: d for d in sub_mgr.get_visible_tool_definitions(is_owner_ctx=True)}
    props = sub_defs["shell"]["parameters"].get("properties") or {}
    assert "require_approval" not in props
    assert "approval_reason" not in props


def test_daily_persona_omits_approval_meta_even_when_user_facing(tmp_path: Path) -> None:
    """daily「记录」会话 user_facing=True，仍不注入 require_approval（门已按 persona 跳过）。"""
    from types import SimpleNamespace

    from src.tools.builtin.runtime.shell import ShellTool

    daily = SimpleNamespace(
        delegate_depth=0,
        identity=SimpleNamespace(user_facing=True, persona=SimpleNamespace(name="daily")),
    )
    mgr = ToolManager(owner=daily)
    mgr.register_tool(ShellTool(workspace_root=tmp_path))
    defs = {d["name"]: d for d in mgr.get_visible_tool_definitions(is_owner_ctx=True)}
    props = defs["shell"]["parameters"].get("properties") or {}
    assert "require_approval" not in props
    assert "approval_reason" not in props
