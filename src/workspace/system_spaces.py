"""系统空间登记（门面三形态导航骨架）"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.workspace.types import ViewCapability

# (目录名, 条目名, content_type, 门面, 主页, persona, 摘要)
_INTERNAL_SPACES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "config",
        "配置",
        "config",
        "storefront",
        "/config",
        "config-assistant",
        "系统配置（系统营业空间，表单即编辑入口）",
    ),
    ("review", "消息", "updates", "display", "/review", "review-assistant", "消息中心（系统展示空间，跨空间动态聚合）"),
)

# 「更高级」的身份。目录在用户空间区，不在 .internal/ 系统目录。
_CREATOR_WORKFLOW = (
    "flow",
    "工作流",
    "workflow",
    "storefront",
    "/workflow",
    "flow-root",
    "工作流示例空间（画布即编辑入口，出厂参考实现）",
)

# 记录空间与 daily 合并前的旧独立条目路径（占位目录名）：合并后 daily 条目
# 自带 records 身份，旧条目是重复席位，启动时一次性从注册表清除（目录留盘）
_LEGACY_RECORDS_DIRNAME = "records"


def _internal_space_dir(root: Any, name: str) -> Path | None:
    """internal 系统空间的占位目录：``<coara_home>/workspaces/.internal/<name>``。

    coara_home 从 workspace_manager 取（root 自身不挂该属性，见 daily/usage 用法）。
    """
    wm = getattr(root, "workspace_manager", None)
    home = getattr(wm, "coara_home", None) if wm is not None else None
    if home is None:
        return None
    return Path(home) / "workspaces" / ".internal" / name


def cleanup_legacy_records_workspace(root: Any) -> None:
    """存量迁移：记录空间已并入 daily 条目（daily_curator.ensure_daily_workspace_entry），
    把合并前登记的独立 .internal/records 旧条目从注册表删除（幂等；目录留盘不删）。
    """
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return
    legacy_path = _internal_space_dir(root, _LEGACY_RECORDS_DIRNAME)
    if legacy_path is None:
        return
    try:
        resolved = legacy_path.expanduser().resolve()
    except OSError:
        return
    for entry in list(wm.registry.document.workspaces.values()):
        try:
            if entry.resolved_path() == resolved:
                wm.registry.remove(entry.id)
                logger.info(f"Removed legacy records workspace entry {entry.name} ({entry.id})")
                break
        except OSError:
            continue


def ensure_system_workspace_entries(root: Any) -> None:
    """登记配置/消息两个 internal 系统空间（幂等）。

    空间身份单真源=space.yaml（09-29 裁决不做双写）：出厂把身份落进
    占位目录的 space.yaml，注册表只留 kind/view/summary/persona 等
    登记态字段，身份字段（content_type/storefront/home_view）不再写。
    """
    from src.workspace.identity import write_space_identity

    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return
    for dir_name, name, content_type, storefront, home_view, persona, summary in _INTERNAL_SPACES:
        path = _internal_space_dir(root, dir_name)
        if path is None:
            return
        path.mkdir(parents=True, exist_ok=True)
        write_space_identity(
            path,
            space_type=content_type,
            storefront=storefront,
            home_view=home_view,
        )
        entry = wm.registry.ensure_internal_workspace(
            path,
            name=name,
            view=ViewCapability.WEB_ONLY,
            summary=summary,
            persona=persona,
        )
        logger.info(f"Registered system workspace {name} ({entry.id}) -> {path}")


def _creator_space_dir(root: Any, dir_name: str) -> Path | None:
    """创作者空间的目录：``<coara_home>/workspaces/<dir_name>``（用户空间区，非系统目录）。"""
    wm = getattr(root, "workspace_manager", None)
    home = getattr(wm, "coara_home", None) if wm is not None else None
    if home is None:
        return None
    return Path(home) / "workspaces" / dir_name


def _migrate_legacy_internal_flow(root: Any) -> None:
    """存量迁移：把 .internal/flow 旧 internal 条目从注册表删除（幂等；目录留盘不删）"""
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return
    legacy_path = _internal_space_dir(root, "flow")
    if legacy_path is None:
        return
    try:
        resolved = legacy_path.expanduser().resolve()
    except OSError:
        return
    for entry in list(wm.registry.document.workspaces.values()):
        try:
            if entry.resolved_path() == resolved and entry.kind.value == "internal":
                wm.registry.remove(entry.id)
                logger.info(f"Removed legacy internal flow workspace entry {entry.name} ({entry.id})")
                break
        except OSError:
            continue


def ensure_creator_workflow_entry(root: Any) -> None:
    """登记工作流创作者空间（幂等）：用户空间区的 NORMAL 条目 + 创作者元数据

    空间身份单真源=space.yaml：创作者身份落目录的 space.yaml，注册表
    条目不再持 content_type/storefront/home_view 字段。
    """
    from src.workspace.identity import write_space_identity
    from src.workspace.types import WorkspaceKind

    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return
    _migrate_legacy_internal_flow(root)
    dir_name, name, content_type, storefront, home_view, persona, summary = _CREATOR_WORKFLOW
    path = _creator_space_dir(root, dir_name)
    if path is None:
        return
    path.mkdir(parents=True, exist_ok=True)
    write_space_identity(
        path,
        space_type=content_type,
        storefront=storefront,
        home_view=home_view,
    )
    resolved = path.expanduser().resolve()

    # 复用同路径条目则原地对齐创作者字段；否则新建 NORMAL 条目
    existing = None
    for e in wm.registry.document.workspaces.values():
        try:
            if e.resolved_path() == resolved:
                existing = e
                break
        except OSError:
            continue
    if existing is not None:
        changed = False
        if existing.kind != WorkspaceKind.NORMAL:
            existing.kind = WorkspaceKind.NORMAL
            changed = True
        for field, value in (
            ("persona", persona),
            ("view", ViewCapability.WEB_ONLY),
        ):
            if getattr(existing, field, None) != value:
                setattr(existing, field, value)
                changed = True
        if summary.strip() and not existing.summary.strip():
            existing.summary = summary.strip()
            changed = True
        if changed:
            wm.registry.save()
        logger.info(f"Creator workflow workspace {existing.name} ({existing.id}) -> {resolved}")
        return

    entry = wm.registry.ensure_workspace(path, name=name, summary=summary)
    entry.persona = persona
    entry.view = ViewCapability.WEB_ONLY
    wm.registry.save()
    logger.info(f"Registered creator workflow workspace {entry.name} ({entry.id}) -> {resolved}")
