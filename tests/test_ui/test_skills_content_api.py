"""Tests for the skills content API (GET/POST /api/v1/skills/{name}/content)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.core.errors import SkillError
from src.skills.loader import SkillLoader
from src.skills.manager import SkillManager
from src.ui import control_plane
from src.ui.dashboard_handlers import DashboardRestHandlers

_FRONTMATTER = """---
name: demo-skill
description: 演示技能
---

"""

_BODY = """# 演示技能

这是正文。
"""


def _make_handlers_app(handlers: DashboardRestHandlers) -> web.Application:
    app = web.Application()
    handlers.register_routes(app.router)
    return app


def _static_resolver(skill):
    async def _resolve(name, workspace_dir, coara_home=None):
        return skill if name == skill.name else None

    return _resolve


@pytest.fixture
def skill_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp 工作空间 + 一个工作空间级技能 + 隔离的技能管理器。"""
    skill_dir = tmp_path / ".coara" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_FRONTMATTER + _BODY, encoding="utf-8")
    monkeypatch.setattr(control_plane, "_settings_skill_manager", SkillManager())
    return tmp_path


@pytest.mark.asyncio
async def test_read_skill_content_splits_frontmatter_and_body(skill_workspace: Path) -> None:
    handlers = DashboardRestHandlers(skill_workspace)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        resp = await client.get(f"/api/v1/skills/demo-skill/content?token={token}")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "demo-skill"
        assert data["frontmatter_raw"].startswith("---")
        assert "name: demo-skill" in data["frontmatter_raw"]
        assert data["frontmatter_raw"].rstrip().endswith("---")
        assert data["body"].strip() == _BODY.strip()
        assert data["readonly"] is False
        assert data["path"].endswith("SKILL.md")


@pytest.mark.asyncio
async def test_save_skill_content_keeps_frontmatter_replaces_body(skill_workspace: Path) -> None:
    handlers = DashboardRestHandlers(skill_workspace)
    token = handlers.auth_token
    new_body = "# 演示技能\n\n改写后的正文。\n"
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        resp = await client.post(
            f"/api/v1/skills/demo-skill/content?token={token}",
            json={"body": new_body},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    skill_file = skill_workspace / ".coara" / "skills" / "demo-skill" / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    assert text.startswith(_FRONTMATTER)
    assert "改写后的正文。" in text
    assert "这是正文。" not in text
    # 写回后仍可被技能加载器解析
    parsed = SkillLoader.parse(text, str(skill_file))
    assert parsed.name == "demo-skill"
    assert "改写后的正文。" in parsed.body


@pytest.mark.asyncio
async def test_save_builtin_skill_forbidden(skill_workspace: Path) -> None:
    handlers = DashboardRestHandlers(skill_workspace)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        # 内置技能（包内资产）在线保存必须被拒
        resp = await client.post(
            f"/api/v1/skills/event-source/content?token={token}",
            json={"body": "x"},
        )
        assert resp.status == 403


@pytest.mark.asyncio
async def test_save_invalid_skill_content_rejected(skill_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(content: str, location: str):
        raise SkillError(f"Failed to parse skill at {location}: bad frontmatter")

    skill_file = skill_workspace / ".coara" / "skills" / "demo-skill" / "SKILL.md"
    before = skill_file.read_text(encoding="utf-8")
    # 手写一个始终位于内存的技能定义，绕过发现阶段的解析校验（模拟「发现时合法、
    # 保存后内容非法」——校验是保存路径上的守门员，不是发现路径的）
    skill = SkillLoader.parse(_FRONTMATTER + _BODY, str(skill_file))
    monkeypatch.setattr(control_plane, "_resolve_skill", _static_resolver(skill))
    monkeypatch.setattr(SkillLoader, "parse", staticmethod(_boom))
    handlers = DashboardRestHandlers(skill_workspace)
    token = handlers.auth_token
    async with TestClient(TestServer(_make_handlers_app(handlers))) as client:
        resp = await client.post(
            f"/api/v1/skills/demo-skill/content?token={token}",
            json={"body": "非法内容"},
        )
        assert resp.status == 400
        assert "校验未通过" in await resp.text()
    # 校验失败不落盘
    assert skill_file.read_text(encoding="utf-8") == before
