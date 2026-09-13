"""daily internal 空间：注册方式、列表过滤、会话定制点。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.workspace.registry import WorkspaceRegistry
from src.workspace.types import ViewCapability, WorkspaceKind


def _registry(tmp_path: Path) -> WorkspaceRegistry:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    reg = WorkspaceRegistry(home)
    reg.load()
    return reg


def test_ensure_internal_workspace_registers_kind_and_view(tmp_path):
    reg = _registry(tmp_path)
    path = tmp_path / "home" / "workspaces" / ".internal" / "daily"
    path.mkdir(parents=True)

    entry = reg.ensure_internal_workspace(
        path,
        name="daily",
        view=ViewCapability.ALL,
        provider="deepseek",
        model="flash",
        summary="daily 日常整理（系统内部空间）",
    )
    assert entry.kind == WorkspaceKind.INTERNAL
    assert entry.view == ViewCapability.ALL
    assert entry.name == "daily"
    assert entry.provider == "deepseek"
    assert entry.model == "flash"
    # internal 条目绝不占位默认空间
    assert reg.document.default_workspace != entry.id


def test_ensure_internal_workspace_idempotent_and_repairs(tmp_path):
    """重复注册幂等；kind/view 被改动后再次确保会原地修正回系统语义。"""
    reg = _registry(tmp_path)
    path = tmp_path / "home" / "workspaces" / ".internal" / "daily"
    path.mkdir(parents=True)

    first = reg.ensure_internal_workspace(path, name="daily", view=ViewCapability.ALL)
    # 模拟外部改动
    first.kind = WorkspaceKind.MANAGED
    first.view = ViewCapability.WEB_ONLY
    reg.save()

    second = reg.ensure_internal_workspace(path, name="daily", view=ViewCapability.ALL)
    assert second.id == first.id
    assert second.kind == WorkspaceKind.INTERNAL
    assert second.view == ViewCapability.ALL


def test_internal_workspace_hidden_from_default_list_active(tmp_path):
    """internal 空间不进用户常规空间列表（include_internal 才取）。"""
    reg = _registry(tmp_path)
    user_path = tmp_path / "proj"
    user_path.mkdir()
    user_entry = reg.ensure_workspace(user_path)
    internal_path = tmp_path / "home" / "workspaces" / ".internal" / "daily"
    internal_path.mkdir(parents=True)
    reg.ensure_internal_workspace(internal_path, name="daily", view=ViewCapability.ALL)

    default_list = reg.list_active()
    assert [e.id for e in default_list] == [user_entry.id]

    with_internal = reg.list_active(include_internal=True)
    kinds = {e.name: e.kind for e in with_internal}
    assert kinds["daily"] == WorkspaceKind.INTERNAL


def test_daily_workspace_dir_under_coara_home(tmp_path, monkeypatch):
    """daily 空间目录落点：<coara_home>/workspaces/.internal/daily。"""
    from src.records import daily_curator as dc

    home = tmp_path / "coara-home"
    home.mkdir(exist_ok=True)
    root = SimpleNamespace(workspace_manager=SimpleNamespace(coara_home=home))
    assert dc.daily_workspace_dir(root) == home / "workspaces" / ".internal" / "daily"
    # 无 workspace_manager / coara_home 时解析不出
    assert dc.daily_workspace_dir(SimpleNamespace(workspace_manager=None)) is None


def test_ensure_daily_workspace_entry_binds_llm_params(tmp_path, monkeypatch):
    """注册 daily 条目时 provider/model 来自 _daily_llm_params（专属→全局默认）。"""
    from src.records import daily_curator as dc

    home = tmp_path / "coara-home"
    home.mkdir(exist_ok=True)
    reg = WorkspaceRegistry(home)
    reg.load()
    root = SimpleNamespace(workspace_manager=SimpleNamespace(coara_home=home, registry=reg))
    monkeypatch.setattr(dc, "_daily_llm_params", lambda: ("kimi", "k3"))

    entry = dc.ensure_daily_workspace_entry(root)
    assert entry is not None
    assert entry.kind == WorkspaceKind.INTERNAL
    assert entry.view == ViewCapability.ALL
    assert entry.provider == "kimi"
    assert entry.model == "k3"
    # 记录空间与 daily 合并：展示名=记录，persona=daily，身份字段齐备
    assert entry.name == "记录"
    assert entry.persona == "daily"
    assert entry.content_type == "records"
    assert entry.storefront == "display"
    assert entry.home_view == "/records"
    # 工作目录已建
    assert entry.resolved_path().is_dir()


def test_ensure_daily_workspace_entry_renames_legacy_entry(tmp_path, monkeypatch):
    """合并幂等迁移：已有 name=daily 的旧条目原地改名「记录」并补身份（id/path 不动）。"""
    from src.records import daily_curator as dc

    home = tmp_path / "coara-home"
    home.mkdir(exist_ok=True)
    reg = WorkspaceRegistry(home)
    reg.load()
    path = home / "workspaces" / ".internal" / "daily"
    path.mkdir(parents=True)
    legacy = reg.ensure_internal_workspace(path, name="daily", view=ViewCapability.ALL)
    assert legacy.name == "daily"

    root = SimpleNamespace(workspace_manager=SimpleNamespace(coara_home=home, registry=reg))
    monkeypatch.setattr(dc, "_daily_llm_params", lambda: (None, None))
    entry = dc.ensure_daily_workspace_entry(root)

    assert entry.id == legacy.id
    assert entry.resolved_path() == legacy.resolved_path()
    assert entry.name == "记录"
    assert entry.persona == "daily"
    assert entry.home_view == "/records"
    # 再次调用幂等（不再变化）
    again = dc.ensure_daily_workspace_entry(root)
    assert again.id == entry.id and again.name == "记录"


@pytest.mark.asyncio
async def test_dispatch_requires_workspace_session(tmp_path, monkeypatch):
    """空间会话不可用时派发失败返回 None（不坠落 inflight）。"""
    from src.records import daily_curator as dc
    from src.records.agent_store import MemoryStore

    store = MemoryStore(tmp_path / "memory")
    root = SimpleNamespace(records_store=SimpleNamespace(agent=store, agent_root=store.root, user=None))

    async def _ensure(r):
        return None

    monkeypatch.setattr(dc, "ensure_daily_workspace_session", _ensure)
    monkeypatch.setattr(dc, "curator_enabled", lambda: True)

    assert await dc.dispatch_daily(root) is None
    assert dc._curator_flight["task_id"] is None
