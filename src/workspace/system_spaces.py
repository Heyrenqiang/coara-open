"""系统空间登记（门面三形态导航骨架）。

一切皆是空间（docs/空间模型与内容注册表.md §2/§3）：对话空间与系统空间本质
等同，只是门面不同。本模块把 web 侧的系统页面（配置/消息/工作流）登记为
kind=internal 的空间条目——侧边栏因此可以统一从注册表渲染，不再手写功能导航。

- 配置：storefront 营业（表单即编辑入口），主页 /config
- 消息：display 橱窗（消息流只读/处置），主页 /review
- 工作流：storefront 营业（画布即编辑入口），主页 /workflow，示例空间

注：记录空间与 daily 合并登记在 src/records/daily_curator.py（条目名=记录、
persona=daily、主页 /records），本模块不再重复登记。

注：工作流以「示例工作空间」身份在册（本模块登记）。它与其它系统空间同构
（persona=flow-root + 专属画布页面 /workflow），是出厂自带的参考实现——
用户可照此范式构建自己的空间（自定义 schema、独立引擎、内核工具、专属页面）。
执行层不在内核：WDL 实例运行由独立 wdl 软件承载（docs/空间模型与内容注册表.md §1）。

内容是 report 型虚拟文档（查询/聚合生成），条目目录只是占位——统一指向
``<coara_home>/workspaces/.internal/<name>``（与 daily 一致），不放业务文件。
全部登记幂等：重复启动只原地校正身份字段，不重复建条目、不覆盖其它字段。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.workspace.types import ViewCapability

# (目录名, 条目名, content_type, 门面, 主页, persona, 摘要)
_INTERNAL_SPACES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    ("config", "配置", "config", "storefront", "/config", "config-assistant",
     "系统配置（系统营业空间，表单即编辑入口）"),
    ("review", "消息", "updates", "display", "/review", "review-assistant",
     "消息中心（系统展示空间，跨空间动态聚合）"),
)

# 创作者空间（工作流）：开发者给用户的示例工作空间。kind 用普通 MANAGED（可移出、
# 可删、归属用户），靠 content_type=workflow + home_view + persona + view 表达
# 「更高级」的身份。目录在用户空间区，不在 .internal/ 系统目录。
_CREATOR_WORKFLOW = ("flow", "工作流", "workflow", "storefront", "/workflow", "flow-root",
                     "工作流示例空间（画布即编辑入口，出厂参考实现）")

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
    """登记配置/消息两个 internal 系统空间（幂等）。"""
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return
    for dir_name, name, content_type, storefront, home_view, persona, summary in _INTERNAL_SPACES:
        path = _internal_space_dir(root, dir_name)
        if path is None:
            return
        path.mkdir(parents=True, exist_ok=True)
        entry = wm.registry.ensure_internal_workspace(
            path,
            name=name,
            view=ViewCapability.WEB_ONLY,
            summary=summary,
            content_type=content_type,
            storefront=storefront,
            home_view=home_view,
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
    """存量迁移：把 .internal/flow 旧 internal 条目从注册表删除（幂等；目录留盘不删）。

    工作流已从 internal 系统空间改为创作者空间（用户空间区的 MANAGED 条目），
    旧的 .internal/flow 条目是重复席位，启动时一次性清除。
    """
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
    """登记工作流创作者空间（幂等）：用户空间区的 MANAGED 条目 + 创作者元数据。

    根逻辑是普通工作空间（可移出/可删/归属用户），但带专属画布主页与 persona，
    是开发者给用户的示例工作空间。先清存量 .internal/flow 旧条目再登记。
    """
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
    resolved = path.expanduser().resolve()

    # 复用同路径条目则原地对齐创作者字段；否则新建 MANAGED 条目
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
        if existing.kind != WorkspaceKind.MANAGED:
            existing.kind = WorkspaceKind.MANAGED
            changed = True
        for field, value in (
            ("content_type", content_type),
            ("storefront", storefront),
            ("home_view", home_view),
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
    for field, value in (
        ("content_type", content_type),
        ("storefront", storefront),
        ("home_view", home_view),
        ("persona", persona),
        ("view", ViewCapability.WEB_ONLY),
    ):
        setattr(entry, field, value)
    wm.registry.save()
    logger.info(f"Registered creator workflow workspace {entry.name} ({entry.id}) -> {resolved}")
