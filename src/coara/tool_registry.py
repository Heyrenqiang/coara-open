"""Shared tool registration for RootCoara and WorkspaceSession"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


def register_root_scoped_tools(
    coara: CoaraBase,
    *,
    ws_parent: CoaraBase | None = None,
) -> None:
    """Register all root-scoped tools on a CoaraBase instance"""
    from src.tools.builtin.manifest import ROOT_PLAN_TOOL_TYPES, SKILL_TOOL_TYPE
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorTool
    from src.tools.builtin.scheduling.event_source import EVENT_SOURCE_TOOL_TYPE
    from src.tools.builtin.ws.ws import WS_TOOL_TYPE

    ws_parent = ws_parent or coara

    # Plan mode tools (enter / plan / exit)
    for tool_type in ROOT_PLAN_TOOL_TYPES:
        coara.register_tool(tool_type(parent_coara=coara))

    # Skill tool (list / activate) — replace to override the one from bootstrap_tools
    coara.register_tool(SKILL_TOOL_TYPE(parent_coara=coara), replace=True)

    # Orchestrator tool (workflow orchestration) — 挂起工具：注册但不暴露，
    # LLM 需要时经 tool(action="activate") 揭示（should_defer=True）
    coara.register_tool(OrchestratorTool(parent_coara=coara))

    # Workspace tool (list / add / remove / use + updates_*)
    coara.register_tool(WS_TOOL_TYPE(parent_coara=ws_parent), replace=True)

    # Event sources (YAML under matters/definitions + live reload on Root)
    coara.register_tool(EVENT_SOURCE_TOOL_TYPE(parent_coara=ws_parent), replace=True)
