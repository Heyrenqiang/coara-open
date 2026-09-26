"""Built-in tool registration manifest."""

from __future__ import annotations

from src.tools.builtin.planning.plan_mode import ROOT_PLAN_TOOL_TYPES
from src.tools.builtin.scheduling.reminder import REMINDER_TOOL_TYPE
from src.tools.builtin.skills.skills import SKILL_TOOL_TYPE
from src.tools.builtin.web import WebSearchTool
from src.tools.builtin.web.web_fetch import WebFetchTool

PROCESS_STATELESS_TOOL_TYPES = (
    WebSearchTool,
    WebFetchTool,
)

__all__ = [
    "PROCESS_STATELESS_TOOL_TYPES",
    "ROOT_PLAN_TOOL_TYPES",
    "REMINDER_TOOL_TYPE",
    "SKILL_TOOL_TYPE",
]
