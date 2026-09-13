"""Built-in tool registration manifest.

PROCESS_STATELESS_TOOL_TYPES: registered once per process (register_builtin_tools).
ROOT_PLAN_MODE_TOOL_TYPES / REMINDER_TOOL_TYPE / SKILL_TOOL_TYPE: Root lifecycle.
Other Root tools (ws, vault, local_search, tool, …) are registered in
root_lifecycle.py and CoaraBase.initialize(). Workflow orchestration (flow
CRUD + WDL drafts) is on the orchestrator tool, registered only after
activating the workflow skill; delegate stays pure delegation.
"""

from __future__ import annotations

from src.tools.builtin.planning.plan_mode import ROOT_PLAN_MODE_TOOL_TYPES
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
    "ROOT_PLAN_MODE_TOOL_TYPES",
    "REMINDER_TOOL_TYPE",
    "SKILL_TOOL_TYPE",
]
