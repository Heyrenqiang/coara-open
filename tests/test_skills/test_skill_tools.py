"""Tests for skill prompt injection and session tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.core.types import SkillDefinition
from src.prompt.builder import PromptBuilder
from src.prompt.yaml_loader import AgentPromptConfig
from src.skills.session import SkillSessionState
from src.tools.builtin.skills.skills import SkillTool


def test_system_prompt_does_not_embed_skill_catalog() -> None:
    template = "## 技能\n\nSee skill(action=list).\n"
    config = AgentPromptConfig(name="root", system_prompt=template)
    prompt = PromptBuilder().set_yaml_config(config).build()
    assert "Workflow planning" not in prompt


@pytest.mark.asyncio
async def test_search_skills_covers_unlisted_and_marks_activated() -> None:
    from src.skills.manager import SkillManager

    coara = MagicMock()
    coara._skill_session = SkillSessionState(activated={"workflow"})
    coara.skill_manager = SkillManager()
    coara.skill_manager._skills = {
        "workflow": SkillDefinition(name="workflow", description="Workflow", location="/w", body=""),
        "ppt": SkillDefinition(name="ppt", description="Slides", location="/p", body="", listed=False),
    }

    tool = SkillTool(parent_coara=coara)

    # 关键词搜索：挂起技能也能搜到，activated 有标记
    result = await tool.create_invocation({"action": "search", "query": "workflow"}).execute()
    assert not result.is_error
    body = str(result.content)
    assert "**workflow**（activated）" in body

    # 空 query：全量目录，挂起技能带标记
    result = await tool.create_invocation({"action": "search"}).execute()
    body = str(result.content)
    assert "**ppt**（挂起）" in body
    assert "**workflow**（activated）" in body

    # 只看不用：search 不激活
    assert "ppt" not in coara._skill_session.activated


def test_inject_skill_list_renders_listed_and_unlisted_sections() -> None:
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.skills.manager import SkillManager

    manager = SkillManager()
    manager._skills = {
        "workflow": SkillDefinition(name="workflow", description="Workflow", location="/w", body=""),
        "ppt": SkillDefinition(name="ppt", description="Slides", location="/p", body="", listed=False),
    }

    prompt = CoaraBase._inject_skill_list(SimpleNamespace(skill_manager=manager), "头 ${COARA_SKILL_LIST} 尾")

    assert "workflow" in prompt
    assert "ppt" in prompt
    assert "挂起技能" in prompt
    # 挂起段只给名字，不带描述
    assert "Slides" not in prompt


def test_loader_parses_listed_flag() -> None:
    from src.skills.loader import SkillLoader

    skill = SkillLoader.parse("---\nname: demo\ndescription: d\nlisted: false\n---\nbody", "/x/SKILL.md")
    assert skill.listed is False

    skill = SkillLoader.parse("---\nname: demo\ndescription: d\n---\nbody", "/x/SKILL.md")
    assert skill.listed is True


@pytest.mark.asyncio
async def test_activate_skill_loads_full_body() -> None:
    from src.skills.manager import SkillManager

    coara = MagicMock()
    coara.message_history = []
    coara._skill_session = SkillSessionState()
    coara._workflow_draft_pending = False
    coara.skill_manager = SkillManager()
    coara.skill_manager._skills["demo"] = SkillDefinition(
        name="demo",
        description="Demo skill",
        location="/demo/SKILL.md",
        body="Step one\nStep two",
    )

    tool = SkillTool(parent_coara=coara)
    inv = tool.create_invocation({"action": "activate", "name": "demo"})
    result = await inv.execute()
    assert not result.is_error
    assert "demo" in coara._skill_session.activated
    assert "<activated_skill" in str(result.content)
    assert "Step one" in str(result.content)


@pytest.mark.asyncio
async def test_activate_skill_requires_root_coara() -> None:
    tool = SkillTool(parent_coara=None)
    inv = tool.create_invocation({"action": "activate", "name": "demo"})
    result = await inv.execute()
    assert result.is_error
    assert "root" in str(result.content).lower()


@pytest.mark.asyncio
async def test_activate_skill_handles_build_failure(monkeypatch) -> None:
    from src.skills.manager import SkillManager

    coara = MagicMock()
    coara.message_history = []
    coara._skill_session = SkillSessionState()
    coara._workflow_draft_pending = False
    coara.skill_manager = SkillManager()
    coara.skill_manager._skills["demo"] = SkillDefinition(
        name="demo",
        description="Demo skill",
        location="/demo/SKILL.md",
        body="full body",
    )

    def _boom(_skill):
        raise RuntimeError("corrupt skill body")

    monkeypatch.setattr("src.skills.activation.build_activated_skill_reminder", _boom)

    tool = SkillTool(parent_coara=coara)
    inv = tool.create_invocation({"action": "activate", "name": "demo"})
    result = await inv.execute()
    assert result.is_error
    assert "demo" in str(result.content)
    assert "失败" in str(result.content)


@pytest.mark.asyncio
async def test_skill_tool_uses_write_lock() -> None:
    tool = SkillTool(parent_coara=None)
    # skill 切换会话状态，需串行：返回固定锁键
    assert tool.get_write_lock({"action": "activate", "name": "demo"}) == "skill"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add", "remove"])
async def test_legacy_skill_actions_rejected(action: str) -> None:
    tool = SkillTool(parent_coara=MagicMock())
    with pytest.raises(ValueError, match="Unknown skill action"):
        tool.create_invocation({"action": action, "name": "demo"})
