"""Root skill management — list / activate session skills."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


class _SkillSessionTool(BaseTool):
    # Session-scoped (activated flags); must not use the global THINK tool cache.
    kind = ToolKind.OTHER
    category = "skill"
    owner_only = True

    def __init__(self, parent_coara: CoaraBase | None = None):
        super().__init__()
        self._coara = parent_coara

class _SearchSkillsInvocation(ToolInvocation):
    """Search ALL skills (listed + unlisted) by keyword; peek only, no activation."""

    def __init__(self, params: dict[str, Any], coara: CoaraBase | None):
        super().__init__(params)
        self._coara = coara

    def get_description(self) -> str:
        return f"Search skills: {self.params.get('query', '')}"

    async def execute(self, signal=None) -> ToolResult:
        query = str(self.params.get("query", "")).strip().lower()
        if self._coara is None:
            return ToolResult.error("search requires a bound coara instance")
        all_skills = sorted(self._coara.skill_manager.get_all(), key=lambda s: s.name)
        if not all_skills:
            return ToolResult.success("当前无可用技能。")

        if not query:
            matched = [(s, 0) for s in all_skills]
        else:
            keywords = query.split()
            matched = []
            for skill in all_skills:
                text = f"{skill.name.lower()} {skill.description.lower()}"
                score = 0
                for kw in keywords:
                    if kw in skill.name.lower():
                        score += 10
                    elif kw in text:
                        score += 3
                if score > 0:
                    matched.append((skill, score))
            matched.sort(key=lambda pair: -pair[1])

        matched = matched[:10]
        if not matched:
            return ToolResult.success(f"未找到与 {query} 匹配的技能。")

        activated = self._coara._skill_session.activated if self._coara is not None else set()
        lines = [f"找到 {len(matched)} 个匹配技能：", ""]
        for skill, _ in matched:
            flags: list[str] = []
            if not skill.listed:
                flags.append("挂起")
            if skill.name in activated:
                flags.append("activated")
            flag_text = f"（{'、'.join(flags)}）" if flags else ""
            lines.append(f"- **{skill.name}**{flag_text} — {skill.description}")
        return ToolResult.success("\n".join(lines))


class _ActivateSkillInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], coara: CoaraBase | None):
        super().__init__(params)
        self._coara = coara
        self.skill_name = str(params.get("name") or "").strip()

    def get_description(self) -> str:
        return f"Activate skill: {self.skill_name}"

    async def execute(self, signal=None) -> ToolResult:
        if self._coara is None:
            return ToolResult.error("activate_skill requires root coara context")
        from src.skills.activation import execute_skill_activation

        return execute_skill_activation(self.skill_name, self._coara)


class SkillTool(_SkillSessionTool):
    name = "skill"
    display_name = "Skill"
    description = """领域操作指南（SKILL.md），不修改磁盘上的技能文件

| action | 用途 |
|--------|------|
| `search` | 在全部技能（含挂起）中按关键词查找，返回候选 name + description（只看不用）；query 留空返回全量 |
| `activate` | 加载指定技能完整 SKILL.md，必须严格遵照；上下文中已有则无需重复调用 |

领域任务：优先 activate 技能完成。已列出技能：直接 `activate`；挂起技能：先 `search` 看描述再 `activate`
已激活状态只在本会话有效，`/new` 后需重新 activate"""
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "activate"],
                "description": "search=全量关键词查找；activate=加载完整 SKILL.md",
            },
            "name": {
                "type": "string",
                "description": "activate 时的技能名；search 不需要",
            },
            "query": {
                "type": "string",
                "description": "search 用：搜索关键词；留空返回全量目录",
            },
        },
        "required": ["action"],
    }

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        action = str(params.get("action") or "").strip()
        if action == "search":
            return _SearchSkillsInvocation(params, self._coara)
        if action == "activate":
            if not params.get("name"):
                raise ValueError("activate requires name")
            return _ActivateSkillInvocation({"name": params["name"]}, self._coara)
        raise ValueError(f"Unknown skill action: {action}. Use search or activate.")

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        # 会话级状态切换，串行
        return "skill"


SKILL_TOOL_TYPE = SkillTool
