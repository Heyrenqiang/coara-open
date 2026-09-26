"""系统空间登记与存量迁移（记录空间与 daily 合并）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.workspace.registry import WorkspaceRegistry
from src.workspace.system_spaces import (
    cleanup_legacy_records_workspace,
    ensure_system_workspace_entries,
)
from src.workspace.types import ViewCapability, WorkspaceKind


def _root(tmp_path: Path) -> tuple[SimpleNamespace, WorkspaceRegistry]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    reg = WorkspaceRegistry(home)
    reg.load()
    wm = SimpleNamespace(coara_home=home, registry=reg)
    return SimpleNamespace(workspace_manager=wm), reg


def test_system_spaces_register_config_and_review_only(tmp_path: Path) -> None:
    """记录不再由 system_spaces 登记（合并进 daily 条目）；配置/消息照常登记。"""
    root, reg = _root(tmp_path)
    ensure_system_workspace_entries(root)

    by_name = {e.name: e for e in reg.document.workspaces.values()}
    assert "配置" in by_name
    assert "消息" in by_name
    assert "记录" not in by_name
    assert by_name["配置"].kind == WorkspaceKind.INTERNAL
    assert by_name["消息"].home_view == "/review"


def test_cleanup_legacy_records_workspace_removes_old_entry(tmp_path: Path) -> None:
    """存量迁移：.internal/records 旧条目一次性从注册表删除（目录留盘，幂等）。"""
    root, reg = _root(tmp_path)
    home = Path(root.workspace_manager.coara_home)
    legacy_dir = home / "workspaces" / ".internal" / "records"
    legacy_dir.mkdir(parents=True)
    legacy = reg.ensure_internal_workspace(
        legacy_dir,
        name="记录",
        view=ViewCapability.WEB_ONLY,
        content_type="records",
        storefront="display",
        home_view="/records",
    )
    assert reg.get_by_id(legacy.id) is not None

    cleanup_legacy_records_workspace(root)
    assert reg.get_by_id(legacy.id) is None
    # 目录留盘不删
    assert legacy_dir.is_dir()
    # 幂等：再跑无副作用
    cleanup_legacy_records_workspace(root)
    assert reg.get_by_id(legacy.id) is None


def test_cleanup_leaves_daily_records_entry_untouched(tmp_path: Path) -> None:
    """合并后的记录空间条目（.internal/daily，persona=daily）不受迁移清理影响。"""
    root, reg = _root(tmp_path)
    home = Path(root.workspace_manager.coara_home)
    daily_dir = home / "workspaces" / ".internal" / "daily"
    daily_dir.mkdir(parents=True)
    merged = reg.ensure_internal_workspace(
        daily_dir,
        name="记录",
        view=ViewCapability.ALL,
        content_type="records",
        storefront="display",
        home_view="/records",
        persona="daily",
    )

    cleanup_legacy_records_workspace(root)
    assert reg.get_by_id(merged.id) is not None
    assert reg.get_by_id(merged.id).persona == "daily"
