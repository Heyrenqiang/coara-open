"""Programmatic skill activation (shared by skill(action=activate) and routing hooks)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.core.errors import SkillNotFoundError
from src.core.logger import logger
from src.core.message_tags import system_reminder
from src.core.types import SkillDefinition

if TYPE_CHECKING:
    # 类型豁免：CoaraBase 是会话能力的结构类型（register_tool/has_active_turn 等），
    # 仅用于注解、运行期零依赖。skills 被内核注入会话时消费该接口，非反向依赖实现。
    from src.coara.base import CoaraBase


def format_skill_body(skill: SkillDefinition) -> str:
    parts = [
        f'<activated_skill name="{skill.name}">',
        "  <instructions>",
        skill.body,
        "  </instructions>",
        f"  <location>{skill.location}</location>",
    ]
    if skill.references:
        parts.append("  <references>")
        for ref in skill.references:
            parts.append(f"    <ref>{ref}</ref>")
        parts.append("  </references>")
    parts.append("</activated_skill>")
    return "\n".join(parts)


def build_activated_skill_reminder(
    skill: SkillDefinition,
    *,
    preamble: str = "",
    postscript: str = "",
) -> str:
    intro = preamble or f"技能 {skill.name} 已激活。请严格遵循以下完整指南执行相关任务。"
    extra = postscript
    return system_reminder(f"{intro}{extra}\n\n{format_skill_body(skill)}")


def execute_skill_activation(skill_name: str, coara: CoaraBase | None):
    """Activate a skill and return a ToolResult (used by ``skill(action=activate)``)."""
    from src.core.tool_base import ToolResult

    clean = skill_name.strip()
    if not clean:
        return ToolResult.error("activate requires name")
    if coara is None:
        return ToolResult.error("activate requires a bound coara instance")

    try:
        skill = coara.skill_manager.get(clean)
    except SkillNotFoundError:
        available = ", ".join(s.name for s in coara.skill_manager.get_all())
        return ToolResult.error(f"Skill '{clean}' not found. Available skills: {available or 'none'}")

    try:
        coara._skill_session.activated.add(clean)

        content = build_activated_skill_reminder(skill)
        return ToolResult.success(
            content=content,
            metadata={
                "skill_name": clean,
                "location": skill.location,
            },
        )
    except Exception:
        logger.exception("Skill activation failed: {}", clean)
        return ToolResult.error(f"加载技能 {clean} 完整指南失败，请检查系统日志")
