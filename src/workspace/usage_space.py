"""用量空间登记（展示门面试点）"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.workspace.types import ViewCapability

USAGE_WORKSPACE_NAME = "用量"


def usage_workspace_dir(root: Any) -> Path | None:
    """用量空间的工作目录 = coara_home 下 usage 槽位（全局账本汇总）。

    coara_home 从 workspace_manager 取（root 自身不挂该属性，见 root.py 用法）。
    """
    wm = getattr(root, "workspace_manager", None)
    coara_home = getattr(wm, "coara_home", None) if wm is not None else None
    if coara_home is None:
        return None
    return Path(coara_home) / "usage"


def ensure_usage_workspace_entry(root: Any) -> Any | None:
    """确保用量以 kind=internal + 展示门面登记在册（registry 单一事实源）。"""
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return None
    path = usage_workspace_dir(root)
    if path is None:
        return None
    path.mkdir(parents=True, exist_ok=True)
    entry = wm.registry.ensure_internal_workspace(
        path,
        name=USAGE_WORKSPACE_NAME,
        view=ViewCapability.WEB_ONLY,
        summary="用量看板（系统展示空间，全局账本聚合）",
        content_type="usage",
        storefront="display",
        home_view="/usage",
    )
    logger.info(f"Registered usage workspace ({entry.id}) -> {path}")
    return entry
