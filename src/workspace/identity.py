"""空间身份：门面形态与主页判定"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel


class Storefront(StrEnum):
    """门面形态：主页版式与主入口。"""

    WAREHOUSE = "warehouse"  # 闭门运作：对话是主业务，主页带空间身份+进对话
    DISPLAY = "display"  # 橱窗：只读呈现，对话退到辅助位
    STOREFRONT = "storefront"  # 营业：页面直接改内容，表单即编辑入口


class SpaceIdentity(BaseModel):
    """space.yaml 解析结果（可缺省）。全字段缺省 = 纯文件空间继承类型默认。

    《空间能力系统》裁决（09-29）：space.yaml 是空间声明的磁盘单真源，
    不做双写——身份与能力声明跟目录走，注册表只回答「系统里有哪些空间」。
    """

    type: str | None = None
    storefront: Storefront | None = None
    view: str | None = None
    # 主页插件入口：`plugin:bundle.js` → 主页由空间 `.coara/ui/` 下的
    # 插件 bundle 整页渲染（壳提供 DOM 挂载点与 ctx）。非 plugin: 前缀
    # 的取值保留给既有系统路由（/workflow 等）。
    home_view: str | None = None
    # 空间级能力声明：None=不声明=全量默认；声明=白名单。
    # 工具白名单不覆盖核心集（见 src/coara/workspace_capabilities.py）；
    # 技能白名单外视同挂起（仍可 skill search/activate）。
    tools: list[str] | None = None
    skills: list[str] | None = None


# 内容类型默认门面（注册表雏形，先静态 dict）。键 = 内容类型标识。
_TYPE_DEFAULT_STOREFRONT: dict[str, Storefront] = {
    "code": Storefront.WAREHOUSE,
    "usage": Storefront.DISPLAY,
    "config": Storefront.STOREFRONT,
}


def load_space_identity(workspace_dir: Path) -> SpaceIdentity | None:
    """读取空间根目录的 space.yaml；缺失或解析失败返回 None（= 全继承默认）。"""
    path = Path(workspace_dir) / "space.yaml"
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return SpaceIdentity.model_validate(raw)
    except Exception:
        return None


def resolve_home_view(
    workspace_dir: Path,
    *,
    content_type: str | None = None,
) -> Storefront:
    """判定空间主页门面形态。

    覆盖链：space.yaml.storefront → 内容类型默认 → 仓库（兜底）。
    """
    identity = load_space_identity(workspace_dir)
    if identity is not None and identity.storefront is not None:
        return identity.storefront
    ctype = content_type or (identity.type if identity else None)
    if ctype and ctype in _TYPE_DEFAULT_STOREFRONT:
        return _TYPE_DEFAULT_STOREFRONT[ctype]
    return Storefront.WAREHOUSE


def space_home_view(workspace_dir: Path) -> str:
    """空间主页路由（space.yaml.home_view；单真源，无声明=空串=对话主页）。

    插件主页声明 `plugin:bundle.js` 也走这里下发，前端据此走插件槽位。
    解析失败一律返回空串，不让坏 space.yaml 打挂列表接口。
    """
    identity = load_space_identity(workspace_dir)
    if identity is not None and identity.home_view:
        return identity.home_view.strip()
    return ""


def write_space_identity(
    workspace_dir: Path,
    *,
    space_type: str | None = None,
    storefront: str | None = None,
    view: str | None = None,
    home_view: str | None = None,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
) -> Path:
    """出厂/迁移用：把空间身份声明落进 space.yaml（覆盖写，键序固定）。

    系统空间登记时调用，让目录自包含身份（space.yaml 单真源）。
    """
    data: dict[str, object] = {}
    for key, value in (
        ("type", space_type),
        ("storefront", storefront),
        ("view", view),
        ("home_view", home_view),
        ("tools", tools),
        ("skills", skills),
    ):
        if value is not None:
            data[key] = value
    path = Path(workspace_dir) / "space.yaml"
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path
