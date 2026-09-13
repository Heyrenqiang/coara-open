"""Per-CoaraBase SkillManager isolation (REMAINING_ISSUES #269)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import make_test_coara


def _write_workspace_skill(workspace: Path, name: str, body: str) -> None:
    skill_dir = workspace / ".coara" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\n---\n{body}",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_two_coara_instances_keep_workspace_skills_isolated(tmp_path: Path) -> None:
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    # 同名技能、不同内容：全局单例时代后 discover 者会覆盖前者的池
    _write_workspace_skill(ws_a, "demo", "body-from-A")
    _write_workspace_skill(ws_b, "demo", "body-from-B")

    coara_a = make_test_coara(ws_a, name="CoaraA")
    coara_b = make_test_coara(ws_b, name="CoaraB")

    await coara_a.load_skills()
    await coara_b.load_skills()

    assert coara_a.skill_manager.get("demo").body == "body-from-A"
    assert coara_b.skill_manager.get("demo").body == "body-from-B"

    # 激活读所属实例的池：A 中途激活不因 B 已 discover 而 SkillNotFoundError
    from src.skills.activation import execute_skill_activation

    result = execute_skill_activation("demo", coara_a)
    assert not result.is_error
    assert "body-from-A" in str(result.content)
    assert "demo" in coara_a._skill_session.activated
    assert "demo" not in coara_b._skill_session.activated
