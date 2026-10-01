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
        "ppt": SkillDefinition(name="ppt", description="Slides", location="/p", body=""),
    }
    coara.skill_manager.apply_deferred(["ppt"])

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
        "ppt": SkillDefinition(name="ppt", description="Slides", location="/p", body=""),
    }
    manager.apply_deferred(["ppt"])

    prompt = CoaraBase._inject_skill_list(SimpleNamespace(skill_manager=manager), "头 ${COARA_SKILL_LIST} 尾")

    assert "workflow" in prompt
    assert "ppt" in prompt
    assert "挂起技能" in prompt
    # 挂起段只给名字，不带描述
    assert "Slides" not in prompt


def test_apply_deferred_marks_skills() -> None:
    from src.skills.manager import SkillManager

    manager = SkillManager()
    manager._skills = {
        "a": SkillDefinition(name="a", description="A", location="/a", body=""),
        "b": SkillDefinition(name="b", description="B", location="/b", body=""),
    }
    manager.apply_deferred(["b"])
    assert manager.get("a").listed is True
    assert manager.get("b").listed is False
    # 名单整体替换：再次应用空名单恢复常驻
    manager.apply_deferred([])
    assert manager.get("b").listed is True


def test_loader_ignores_legacy_listed_flag() -> None:
    from src.skills.loader import SkillLoader

    # listed 字段已退役：SKILL.md 里写了也不生效，挂起只看配置 skills.deferred
    skill = SkillLoader.parse("---\nname: demo\ndescription: d\nlisted: false\n---\nbody", "/x/SKILL.md")
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

@pytest.mark.asyncio
async def test_import_skill_copies_from_other_workspace(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from src.skills.manager import SkillManager
    from src.workspace.registry import WorkspaceRegistry

    src_ws = tmp_path / "src_ws"
    skill_dir = src_ws / ".coara" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 演示\n---\n正文内容", encoding="utf-8")
    (skill_dir / "extra.txt").write_text("附件", encoding="utf-8")
    cur_ws = tmp_path / "cur_ws"
    cur_ws.mkdir()

    registry = WorkspaceRegistry(tmp_path / "home")
    registry.document = type(registry.document)()
    registry.ensure_workspace(src_ws, name="源空间")
    registry.ensure_workspace(cur_ws, name="当前")

    root = MagicMock()
    root.workspace_manager.registry = registry
    coara = MagicMock()
    coara._root_ref = root
    coara.workspace_dir = str(cur_ws)
    coara.skill_manager = SkillManager()
    await coara.skill_manager.discover(cur_ws, coara_home=None)
    coara.load_skills = AsyncMock()

    tool = SkillTool(parent_coara=coara)
    result = await tool.create_invocation(
        {"action": "import", "name": "demo-skill", "from_workspace": "源空间"}
    ).execute()
    assert not result.is_error
    dest = cur_ws / ".coara" / "skills" / "demo-skill"
    assert (dest / "SKILL.md").exists()
    assert (dest / "extra.txt").read_text(encoding="utf-8") == "附件"
    assert "源空间" in (dest / ".imported-from").read_text(encoding="utf-8")
    assert "正文内容" in (dest / "SKILL.md").read_text(encoding="utf-8")

    # 覆盖需 overwrite=true
    result = await tool.create_invocation(
        {"action": "import", "name": "demo-skill", "from_workspace": "源空间"}
    ).execute()
    assert result.is_error
    assert "overwrite" in str(result.content)
    result = await tool.create_invocation(
        {"action": "import", "name": "demo-skill", "from_workspace": "源空间", "overwrite": True}
    ).execute()
    assert not result.is_error


@pytest.mark.asyncio
async def test_import_rejects_skill_already_loadable(tmp_path) -> None:
    from src.skills.manager import SkillManager
    from src.workspace.registry import WorkspaceRegistry

    # 源空间造一个与出厂技能同名的技能：当前链已可加载 → 拒绝搬运
    src_ws = tmp_path / "src_ws"
    skill_dir = src_ws / ".coara" / "skills" / "event-source"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: event-source\ndescription: 山寨\n---\nx", encoding="utf-8")
    cur_ws = tmp_path / "cur_ws"
    cur_ws.mkdir()

    registry = WorkspaceRegistry(tmp_path / "home")
    registry.document = type(registry.document)()
    registry.ensure_workspace(src_ws, name="源空间")
    registry.ensure_workspace(cur_ws, name="当前")

    root = MagicMock()
    root.workspace_manager.registry = registry
    coara = MagicMock()
    coara._root_ref = root
    coara.workspace_dir = str(cur_ws)
    coara.skill_manager = SkillManager()
    await coara.skill_manager.discover(cur_ws, coara_home=None)

    tool = SkillTool(parent_coara=coara)
    result = await tool.create_invocation(
        {"action": "import", "name": "event-source", "from_workspace": "源空间"}
    ).execute()
    assert result.is_error
    assert "无需搬运" in str(result.content)
    assert not (cur_ws / ".coara" / "skills" / "event-source").exists()


@pytest.mark.asyncio
async def test_search_scope_all_lists_foreign_workspaces(tmp_path) -> None:
    from src.skills.manager import SkillManager
    from src.workspace.registry import WorkspaceRegistry

    src_ws = tmp_path / "src_ws"
    skill_dir = src_ws / ".coara" / "skills" / "foreign-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: foreign-skill\ndescription: 外部技能\n---\nx", encoding="utf-8")
    cur_ws = tmp_path / "cur_ws"
    cur_ws.mkdir()

    registry = WorkspaceRegistry(tmp_path / "home")
    registry.document = type(registry.document)()
    registry.ensure_workspace(src_ws, name="源空间")
    registry.ensure_workspace(cur_ws, name="当前")

    root = MagicMock()
    root.workspace_manager.registry = registry
    coara = MagicMock()
    coara._root_ref = root
    coara.workspace_dir = str(cur_ws)
    coara._skill_session = SkillSessionState()
    coara.skill_manager = SkillManager()
    await coara.skill_manager.discover(cur_ws, coara_home=None)

    tool = SkillTool(parent_coara=coara)

    # 默认 scope=current：看不到其它空间的技能
    result = await tool.create_invocation({"action": "search", "query": "foreign"}).execute()
    assert "foreign-skill" not in str(result.content)

    # scope=all：列出并标注来源空间
    result = await tool.create_invocation({"action": "search", "query": "foreign", "scope": "all"}).execute()
    body = str(result.content)
    assert "foreign-skill" in body
    assert "源空间" in body
    assert "import" in body
