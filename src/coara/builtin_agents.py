"""Built-in SubAgent registry.

Loads agent configs from src/coara/prompts/agents/{name}.md with YAML frontmatter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.core.logger import logger
from src.prompt.yaml_loader import AgentPromptConfig, YamlPromptLoader


@dataclass
class SubAgentConfig:
    """Configuration for a built-in sub-agent."""

    name: str
    role: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)  # empty = inherit all
    yaml_config: AgentPromptConfig | None = None


def _md_agent_path(name: str) -> Path:
    """Return the path to the agent Markdown config file (with YAML frontmatter)."""
    return Path(__file__).parent / "prompts" / "agents" / f"{name}.md"


def _load_from_md(name: str) -> SubAgentConfig | None:
    """Load subagent config from paired ``.yaml`` + ``.md`` under prompts/agents/."""
    md_path = _md_agent_path(name)
    if not md_path.exists():
        return None

    yaml_path = md_path.with_suffix(".yaml")
    if not yaml_path.exists():
        logger.warning(f"Built-in subagent '{name}' missing paired YAML config: {yaml_path.name}")
        return None

    try:
        md_body = md_path.read_text(encoding="utf-8").strip()
        if not md_body:
            return None

        loader = YamlPromptLoader()
        config = loader.load(yaml_path)
        # Pair with .md body (same as AgentRegistry._load_from_yaml).
        # Conditional includes ({{INCLUDE_IF:var:path}}) are NOT resolved here —
        # they need runtime knowledge (e.g. foreground-channel gating) that only
        # the delegate call knows; ``delegate_system_prompt`` resolves them.
        prefix = config.system_prompt.strip()
        if prefix:
            config.system_prompt = f"{prefix}\n\n{md_body}"
        else:
            config.system_prompt = md_body

        tools: list[str] = []
        if config.tools_include and config.tools_include != ["*"]:
            tools = config.tools_include

        return SubAgentConfig(
            name=name,
            role=config.role or name,
            description=config.when_to_use or "",
            system_prompt=config.system_prompt,
            tools=tools,
            yaml_config=config,
        )
    except Exception as exc:
        logger.warning(f"Failed to load subagent YAML+MD for '{name}': {exc}")
        return None


def _load_builtin_subagents() -> list[SubAgentConfig]:
    """Load all SubAgent configurations from Markdown with YAML frontmatter."""
    subagents = []
    names = ["coaras", "aide", "daily"]

    for name in names:
        md_config = _load_from_md(name)
        if md_config is not None:
            subagents.append(md_config)
        else:
            logger.warning(f"Built-in subagent config not found: {name}")

    return subagents


BUILTIN_SUBAGENTS: list[SubAgentConfig] = _load_builtin_subagents()

# 模块/系统空间专属 persona 的惰性缓存（flow-root、config-assistant 等）。
# 与 delegate 可委派名单分离：它们不是可委派类型，只作空间会话主体。
_PERSONA_CACHE: dict[str, SubAgentConfig | None] = {}


def get_module_persona(name: str) -> SubAgentConfig | None:
    """惰性加载模块空间的专属 persona（flow-root、config-assistant 等）。

    与 :data:`BUILTIN_SUBAGENTS` 分离：这些 persona 不作 delegate 候选，
    只在创建对应空间的会话主体时按名加载（文件齐备即得，不进可委派名单）。
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    if key not in _PERSONA_CACHE:
        _PERSONA_CACHE[key] = _load_from_md(key)
    return _PERSONA_CACHE[key]

# Removed built-in types — delegate rejects these at runtime (see docs/子智能体重构.md)
REMOVED_SUBAGENT_TYPES: frozenset[str] = frozenset({"research", "explore"})

# System-only: loadable via get_subagent / system dispatch, never shown in Root LLM
# delegate schema or description list (janitor / daily are triggered by the runtime —
# /new 概况、消息过目、每日整理 — not delegated by the model).
SYSTEM_ONLY_SUBAGENT_TYPES: frozenset[str] = frozenset({"janitor", "daily"})

# CLI 静默子智能体：工具摘要/改动 diff 不进 scrollback（活动树行也静默）。
# janitor/daily 是系统派发管家——它们的工具输出不面向用户；aide 与 coaras 同机制，正常显示。
CLI_SILENT_SUBAGENT_TYPES: frozenset[str] = frozenset({"janitor", "daily"})


def removed_subagent_type_message(name: str) -> str | None:
    """Error text when delegate uses a removed subagent type."""
    lower = (name or "").strip().lower()
    if lower == "research":
        return (
            "SubAgent 'research' 已移除。多源调研请在 Root 使用 "
            'skill(action="activate", name="research")；'
            '并行子任务请 delegate(subagent_type="coaras")'
        )
    if lower == "explore":
        return (
            "SubAgent 'explore' 已移除。只读摸底与工程子任务统一用 "
            'delegate(subagent_type="coaras")；'
            "只读时在 prompt 中写明禁止修改即可"
        )
    return None


def get_subagent(name: str) -> SubAgentConfig | None:
    """Get a sub-agent config by name (case-insensitive)."""
    lower = (name or "").strip().lower()
    if lower in REMOVED_SUBAGENT_TYPES:
        return None
    for sa in BUILTIN_SUBAGENTS:
        if sa.name.lower() == lower:
            return sa
    return None


def get_enabled_subagents(agent_def_subagents: list[str] | None = None) -> list[SubAgentConfig]:
    """Get enabled sub-agents for an agent (LLM-facing list).

    System-only types (e.g. janitor) are always omitted — they remain loadable via
    ``get_subagent`` for internal dispatch.

    If agent_def_subagents is None, return all user-facing built-in sub-agents.
    If it is an empty list, return empty list (subagents explicitly disabled).
    If provided with names, return only matching valid user-facing sub-agents.
    """
    if agent_def_subagents is None:
        return [sa for sa in BUILTIN_SUBAGENTS if sa.name not in SYSTEM_ONLY_SUBAGENT_TYPES]

    enabled = []
    seen: set[str] = set()
    for name in agent_def_subagents:
        sa = get_subagent(name)
        if sa and sa.name not in seen and sa.name not in SYSTEM_ONLY_SUBAGENT_TYPES:
            enabled.append(sa)
            seen.add(sa.name)
    return enabled
