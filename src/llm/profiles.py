"""Named LLM profile identifiers for routing."""

from __future__ import annotations


class Profile:
    """Stable profile names used across the codebase."""

    AGENT_MAIN = "agent.main"
    AGENT_WEB_SEARCH = "agent.web_search"
    CONTEXT_COMPRESSION = "context.compression"
    WORKFLOW_NODE = "workflow.node"

    DEFAULT = AGENT_MAIN
