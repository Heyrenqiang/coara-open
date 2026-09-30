"""空间级能力声明解析（《空间能力系统》槽位一三横切）。

space.yaml 单真源（09-29 裁决：不做双写）→ 会话装配期的收窄规则。
单一职责：纯函数，不持状态，供 workspace_session 装配与 prompt_skills 技能打戳共用。
"""

from __future__ import annotations

from pathlib import Path

from src.workspace.identity import load_space_identity

# 核心集：回合循环 / 提示词 / 委派链深度引用的工具，恒在、白名单管不到。
# 缺了它们 agent 循环会残（todo park 收尾、delegate 委派、plan 模式、
# skill 技能装载、ws 空间管理、tool 挂起网关），故不可被空间声明裁掉。
CORE_TOOL_NAMES: frozenset[str] = frozenset({"todo", "delegate", "plan", "skill", "ws", "tool"})


def resolve_tool_whitelist(workspace_dir: Path) -> set[str] | None:
    """空间的工具白名单：None=未声明（全量默认）；声明后 = 名单 ∪ 核心集。"""
    identity = load_space_identity(Path(workspace_dir))
    if identity is None or not identity.tools:
        return None
    return set(identity.tools) | set(CORE_TOOL_NAMES)


def resolve_skill_allowed(workspace_dir: Path) -> set[str] | None:
    """空间的技能白名单：None=未声明（全量默认）；声明后只留名单内。"""
    identity = load_space_identity(Path(workspace_dir))
    if identity is None or identity.skills is None:
        return None
    return set(identity.skills)

