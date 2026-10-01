"""Root skill management — list / activate session skills / import across workspaces."""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.logger import logger
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.skills.loader import SkillLoader, load_skills_from_dir

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


def _workspace_skills_dir(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / ".coara" / "skills"


def _all_workspace_entries(coara: CoaraBase) -> list[tuple[str, Path]]:
    """注册表全部空间 → (显示名, 目录)；无管理器时返回空。"""
    root = getattr(coara, "_root_ref", None) or coara
    manager = getattr(root, "workspace_manager", None)
    registry = getattr(manager, "registry", None) if manager is not None else None
    if registry is None:
        return []
    with contextlib.suppress(Exception):
        manager.reload_if_stale()
    return [(entry.name, entry.resolved_path()) for entry in registry.document.workspaces.values()]


class _SearchSkillsInvocation(ToolInvocation):
    """Search skills by keyword; peek only, no activation.

    scope 默认 current（当前加载链：全局级 + 本空间）；scope=all 额外枚举注册表
    所有空间的 .coara/skills/ 并标注来源空间，供挑选后 import。
    """

    def __init__(self, params: dict[str, Any], coara: CoaraBase | None):
        super().__init__(params)
        self._coara = coara

    def get_description(self) -> str:
        return f"Search skills: {self.params.get('query', '')}"

    @staticmethod
    def _match(skills: list, query: str) -> list:
        if not query:
            return [(s, 0) for s in skills]
        keywords = query.split()
        matched = []
        for skill in skills:
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
        return matched

    async def execute(self, signal=None) -> ToolResult:
        query = str(self.params.get("query", "")).strip().lower()
        scope = str(self.params.get("scope", "current")).strip().lower()
        if self._coara is None:
            return ToolResult.error("search requires a bound coara instance")
        all_skills = sorted(self._coara.skill_manager.get_all(), key=lambda s: s.name)
        matched = self._match(all_skills, query)[:10]
        activated = self._coara._skill_session.activated

        lines: list[str] = []
        if matched:
            lines.append(f"当前可用技能 {len(matched)} 个匹配：")
            for skill, _ in matched:
                flags: list[str] = []
                if not skill.listed:
                    flags.append("挂起")
                if skill.name in activated:
                    flags.append("activated")
                flag_text = f"（{'、'.join(flags)}）" if flags else ""
                lines.append(f"- **{skill.name}**{flag_text} — {skill.description}")
        elif scope != "all":
            return ToolResult.success(f"未找到与 {query or '全部'} 匹配的技能。")

        if scope == "all":
            current_names = {s.name for s in all_skills}
            current_dir = Path(self._coara.workspace_dir).resolve()
            foreign: list[tuple[str, Any]] = []
            for ws_name, ws_path in _all_workspace_entries(self._coara):
                if ws_path == current_dir:
                    continue
                skills_dir = _workspace_skills_dir(ws_path)
                if not skills_dir.exists():
                    continue
                try:
                    others = await load_skills_from_dir(skills_dir)
                except Exception as exc:
                    logger.warning(f"search scope=all: skip {skills_dir}: {exc}")
                    continue
                for skill in others:
                    if query:
                        if self._match([skill], query):
                            foreign.append((ws_name, skill))
                    else:
                        foreign.append((ws_name, skill))
            lines.append("")
            lines.append("其它空间的技能（需 import 到当前空间后才能 activate）：")
            if not foreign:
                lines.append("- （无匹配）")
            for ws_name, skill in foreign:
                mark = "（当前空间已有同名）" if skill.name in current_names else ""
                lines.append(f"- **{skill.name}** @ {ws_name}{mark} — {skill.description}")
        return ToolResult.success("\n".join(lines) if lines else "当前无可用技能。")


class _ActivateSkillInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], coara: CoaraBase | None):
        super().__init__(params)
        self._coara = coara
        self.skill_name = str(params.get("name") or "").strip()

    def get_description(self) -> str:
        return f"Activate skill: {self.skill_name}"

    async def execute(self, signal=None) -> ToolResult:
        if self._coara is None:
            return ToolResult.error("activate requires root coara context")
        from src.skills.activation import execute_skill_activation

        return execute_skill_activation(self.skill_name, self._coara)


class _ImportSkillInvocation(ToolInvocation):
    """把其它空间的技能整份拷贝到当前空间 .coara/skills/（复制，不是引用）。

    同名冲突默认拒绝，overwrite=true 才覆盖；已在当前链上的技能提示无需搬运。
    """

    def __init__(self, params: dict[str, Any], coara: CoaraBase | None):
        super().__init__(params)
        self._coara = coara
        self.skill_name = str(params.get("name") or "").strip()
        self.from_ws = str(params.get("from_workspace") or "").strip()
        self.overwrite = bool(params.get("overwrite"))

    def get_description(self) -> str:
        return f"Import skill {self.skill_name} from {self.from_ws}"

    async def execute(self, signal=None) -> ToolResult:
        if self._coara is None:
            return ToolResult.error("import requires a bound coara instance")
        if not self.skill_name or not self.from_ws:
            return ToolResult.error("import requires name and from_workspace")

        current_dir = Path(self._coara.workspace_dir).resolve()
        target_entry: tuple[str, Path] | None = None
        for ws_name, ws_path in _all_workspace_entries(self._coara):
            if ws_name == self.from_ws or ws_path.name == self.from_ws:
                target_entry = (ws_name, ws_path)
                break
        if target_entry is None:
            return ToolResult.error(f"未找到空间「{self.from_ws}」（用注册表中的空间名）")
        source_name, source_path = target_entry
        if source_path == current_dir:
            return ToolResult.error("来源空间就是当前空间，无需搬运")

        src_root = _workspace_skills_dir(source_path)
        src_skill_md = src_root / self.skill_name / "SKILL.md"
        if not src_skill_md.exists():
            return ToolResult.error(f"空间「{source_name}」没有名为「{self.skill_name}」的技能（路径 {src_skill_md}）")

        # 已在当前链上（全局级已有同名）→ 提示，不搬运；本空间副本存在时需 overwrite
        try:
            existing = self._coara.skill_manager.get(self.skill_name)
        except Exception:
            existing = None
        dest_dir = _workspace_skills_dir(current_dir) / self.skill_name
        dest_md = (dest_dir / "SKILL.md").resolve()
        if existing is not None and Path(existing.location).resolve() != dest_md:
            return ToolResult.error(
                f"技能「{self.skill_name}」当前已可加载（{existing.location}），无需搬运；"
                f"如确要用该空间版本，先删除当前链上的同名技能再 import"
            )
        if dest_md.exists() and not self.overwrite:
            return ToolResult.error(f"当前空间已存在同名技能「{self.skill_name}」；确认覆盖请 overwrite=true")

        dest_dir = _workspace_skills_dir(current_dir) / self.skill_name
        src_dir = src_skill_md.parent
        try:
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(src_dir, dest_dir)
            # 副本留来源注记，便于将来追查分叉
            note = dest_dir / ".imported-from"
            note.write_text(f"{source_name}\t{src_dir}\n", encoding="utf-8")
            # 校验副本可解析
            SkillLoader.load_from_file(dest_dir / "SKILL.md")
        except Exception as exc:
            return ToolResult.error(f"拷贝失败：{exc}")

        # 刷新当前技能池与 prompt 缓存，立即可 activate
        try:
            await self._coara.load_skills()
        except Exception as exc:
            logger.warning(f"import 后刷新技能池失败: {exc}")
        return ToolResult.success(
            f"已把技能「{self.skill_name}」从空间「{source_name}」复制到当前空间（{dest_dir}），现在可以 activate 了"
        )


class SkillTool(_SkillSessionTool):
    name = "skill"
    display_name = "Skill"
    description = """领域操作指南（SKILL.md），不修改磁盘上的技能文件

| action | 用途 |
|--------|------|
| `search` | 关键词查技能（只看不用）；query 留空返回全量；scope=current（默认）当前可用，all 含其它空间 |
| `activate` | 加载指定技能完整 SKILL.md，必须严格遵照；只能 activate 当前可用链上的技能，已有则无需重复 |
| `import` | 把其它空间的技能整份复制到当前空间（name + from_workspace 必填；同名需 overwrite=true），复制后即可用 |

领域任务：优先 activate 技能完成。已列出技能：直接 `activate`；挂起技能：先 `search` 看描述再 `activate`
想用别的空间的技能：先 `search(scope=all)` 找到来源空间，再 `import` 复制进来
已激活状态只在本会话有效，`/new` 后需重新 activate"""
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "activate", "import"],
                "description": "search=关键词查找；activate=加载完整 SKILL.md；import=从其它空间复制技能到当前空间",
            },
            "name": {
                "type": "string",
                "description": "activate/import 时的技能名；search 不需要",
            },
            "query": {
                "type": "string",
                "description": "search 用：搜索关键词；留空返回全量目录",
            },
            "scope": {
                "type": "string",
                "enum": ["current", "all"],
                "description": "search 用：current（默认）只查当前可用技能；all 额外列出其它空间的技能并标注来源空间",
            },
            "from_workspace": {
                "type": "string",
                "description": "import 用：来源空间名（注册表中的空间名）",
            },
            "overwrite": {
                "type": "boolean",
                "description": "import 用：当前空间已有同名技能时确认覆盖，默认 false 拒绝",
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
        if action == "import":
            if not params.get("name"):
                raise ValueError("import requires name")
            return _ImportSkillInvocation(params, self._coara)
        raise ValueError(f"Unknown skill action: {action}. Use search / activate / import.")

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        # 会话级状态切换，串行
        return "skill"


SKILL_TOOL_TYPE = SkillTool
