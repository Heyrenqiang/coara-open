"""Prompt 构建与技能 mixin（I 组）。

宿主为 CoaraBase（src/coara/base.py），属性（_static_prompt_cache /
skill_manager / _skills / _skill_session / workspace_dir / _tool_manager /
identity.persona）在宿主 __init__ 初始化（mixin 不设 __init__，只做方法容器）。
"""

from __future__ import annotations

from typing import Any

from src.core.coara_home import resolve_coara_home
from src.core.logger import logger
from src.prompt.builder import PromptBuilder


class PromptSkillsMixin:
    """Prompt 构建与技能：system prompt 组装 / 缓存失效 / 技能装载 / 内置工具引导"""

    def _build_system_prompt(self) -> str:
        """Build system prompt.

        Strategy: Static template rendered once, runtime context appended every turn.
        Environment / subagent info is appended as text rather than injected
        as separate messages, keeping message_history pure conversation.
        """
        # 1. Cache (Static prompt never changes during session)
        if "static" not in self._static_prompt_cache:
            builder = PromptBuilder()
            if self.identity.persona.yaml_config is not None:
                builder.set_yaml_config(self.identity.persona.yaml_config)
            else:
                builder.set_role_prompt(
                    self.identity.persona.system_prompt_template or "You are a helpful AI assistant."
                )
            builder.set_workspace_dir(self.workspace_dir)
            prompt = builder.build()
            prompt = self._inject_skill_list(prompt)
            prompt = self._inject_deferred_tool_list(
                prompt,
                self._tool_manager,
                self.identity.is_owner_context,
            )
            self._static_prompt_cache["static"] = prompt

        prompt = self._static_prompt_cache["static"]

        # 动态段：被禁用的工具明确告知（tools 参数已过滤，这里给模型硬信号避免误调）
        disabled_names = self._tool_manager.get_disabled_names()
        if disabled_names:
            names_text = "、".join(f"`{n}`" for n in disabled_names)
            prompt += (
                "\n\n<系统提醒>以下工具当前已被禁用，禁止调用："
                f"{names_text}。如需恢复请运行 /tools on <名称>。</系统提醒>"
            )
        return prompt

    def _inject_skill_list(self, prompt: str) -> str:
        """Render ``${COARA_SKILL_LIST}`` with discovered skill names (root.md opt-in).

        两段式：listed（人工策展）技能名直接列出；挂起技能（自动生成、未策展）
        只给裸名字并指引用 skill(action="search") 查描述——与挂起工具清单同构。
        子智能体无技能模块：需要技能时由主会话把技能内容写进任务指令。
        """
        if "${COARA_SKILL_LIST}" not in prompt:
            return prompt
        all_skills = self.skill_manager.get_all()
        listed = [s.name for s in all_skills if s.listed]
        unlisted = [s.name for s in all_skills if not s.listed]
        sections: list[str] = []
        sections.append("、".join(listed) if listed else "（无）")
        if unlisted:
            shown, overflow = unlisted[:30], len(unlisted) - 30
            names_text = "、".join(shown) + (f" 等 {len(unlisted)} 个" if overflow > 0 else "")
            sections.append('挂起技能（仅名字，描述用 `skill(action="search")` 查询）：' + names_text)
        return prompt.replace("${COARA_SKILL_LIST}", "\n".join(sections))

    @staticmethod
    def _inject_deferred_tool_list(
        prompt: str,
        tool_manager: Any,
        is_owner_ctx: bool,
    ) -> str:
        """Render ``${COARA_DEFERRED_TOOL_LIST}`` — deferred built-in tools.

        Deferred tools get name + one-line description. The listing is static
        for the whole session: revealed tools stay listed so the system prompt
        prefix never changes on activation (prompt-cache friendly).
        """
        if "${COARA_DEFERRED_TOOL_LIST}" not in prompt:
            return prompt

        deferred = tool_manager.get_deferred_tool_summaries(is_owner_ctx, include_revealed=True)

        if deferred:
            listing = '挂起的内置工具（`tool(action="activate", name="…")` 装载后即可调用）：\n' + "\n".join(
                f"- `{item['name']}` — {item['description']}" for item in deferred
            )
        else:
            listing = "（当前无挂起工具）"
        return prompt.replace("${COARA_DEFERRED_TOOL_LIST}", listing)

    def _invalidate_prompt_cache(self) -> None:
        self._static_prompt_cache.clear()
        self._tool_manager.invalidate_cache()

    async def load_skills(self, only: list[str] | None = None) -> None:
        """Load discovered skills for the current workspace.

        When ``only`` is provided, restrict the loaded skills to those whose
        ``name`` appears in the list. ``None`` (default) loads all discovered
        skills, preserving existing behavior for Root/sub-agents.
        """
        await self.skill_manager.discover(self.workspace_dir, coara_home=resolve_coara_home(self.workspace_dir))
        all_skills = self.skill_manager.get_all()
        if only is None:
            self._skills = all_skills
        else:
            only_set = set(only)
            self._skills = [s for s in all_skills if s.name in only_set]
        logger.debug(f"Loaded {len(self._skills)} skills for {self.identity.name}")
        self._invalidate_prompt_cache()

    async def bootstrap_tools(self, *, with_skill_tool: bool = True) -> None:
        from src.tools import register_builtin_tools
        from src.tools.builtin.skills.skills import SkillTool
        from src.tools.registry import tool_registry

        register_builtin_tools()
        self.register_tools(tool_registry.list_all())
        if with_skill_tool:
            self.register_tool(SkillTool(parent_coara=self))
