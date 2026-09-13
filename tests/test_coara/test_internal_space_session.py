"""internal 系统空间的 WorkspaceSession 创建：persona 语义回归。

根因（09-09 用量/消息/配置切换 500）：persona_key 曾以展示名兜底
（``entry.persona or entry.name``），无 persona 的 internal 空间拿中文
展示名去查内置 agent 注册表必落空 → RuntimeError → HTTP 500。
约定：persona 显式指定才查注册表；未指定的 internal 空间用 root 身份对话。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.types import ViewCapability, WorkspaceKind


def _register_internal(root, name: str, path: Path, *, persona: str | None = None):
    entry = root.workspace_manager.registry.ensure_internal_workspace(
        path,
        name=name,
        view=ViewCapability.WEB_ONLY,
        summary="t",
        content_type="t",
        storefront="display",
        home_view="/t",
    )
    if persona is not None:
        entry.persona = persona
        root.workspace_manager.registry.save()
    return entry


@pytest.mark.asyncio
async def test_internal_space_without_persona_uses_root_identity(tmp_path: Path) -> None:
    """用量/消息/配置型空间（无 persona）：会话创建成功，主体是 root 身份。"""
    from tests.real_env_helpers import managed_initialized_root

    async with managed_initialized_root(tmp_path) as root:
        space_dir = tmp_path / "home" / "workspaces" / ".internal" / "usage"
        space_dir.mkdir(parents=True)
        entry = _register_internal(root, "用量", space_dir)
        assert entry.kind == WorkspaceKind.INTERNAL

        session = await root.ensure_workspace_session(entry)
        assert session.coara.identity.name == root.identity.name


@pytest.mark.asyncio
async def test_internal_space_with_daily_persona_uses_daily_identity(tmp_path: Path) -> None:
    """记录型空间（persona=daily）：会话主体仍是 daily。"""
    from tests.real_env_helpers import managed_initialized_root

    async with managed_initialized_root(tmp_path) as root:
        space_dir = tmp_path / "home" / "workspaces" / ".internal" / "daily"
        space_dir.mkdir(parents=True)
        entry = _register_internal(root, "记录", space_dir, persona="daily")

        session = await root.ensure_workspace_session(entry)
        assert session.coara.identity.name == "daily"


@pytest.mark.asyncio
async def test_internal_space_with_unknown_persona_fails_loud(tmp_path: Path) -> None:
    """persona 显式指定但注册表没有：仍要 fail-loud，不静默降级。"""
    from tests.real_env_helpers import managed_initialized_root

    async with managed_initialized_root(tmp_path) as root:
        space_dir = tmp_path / "home" / "workspaces" / ".internal" / "ghost"
        space_dir.mkdir(parents=True)
        entry = _register_internal(root, "幽灵", space_dir, persona="ghost-agent")

        with pytest.raises(RuntimeError, match="missing builtin agent config"):
            await root.ensure_workspace_session(entry)
