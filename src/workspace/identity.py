"""空间身份：门面形态与主页判定。

对应 docs/空间模型与内容注册表.md §3。门面形态（storefront）由「用户介入内容的
方式」决定：仓库=对话下令运作、展示=只读呈现、营业=页面直接改。判定走覆盖链：
内容类型默认 → space.yaml 覆盖（可缺省，缺省即继承类型默认）。

space.yaml 落在空间根目录，字段仅三个：type / storefront / view。v8 这类纯文件
空间不写 space.yaml 也天然是仓库（类型默认）。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel


class Storefront(StrEnum):
    """门面形态：主页版式与主入口。"""

    WAREHOUSE = "warehouse"  # 闭门运作：对话是主业务，主页带空间身份+进对话
    DISPLAY = "display"      # 橱窗：只读呈现，对话退到辅助位
    STOREFRONT = "storefront"  # 营业：页面直接改内容，表单即编辑入口


class SpaceIdentity(BaseModel):
    """space.yaml 解析结果（可缺省）。全字段缺省 = 纯文件空间继承类型默认。"""

    type: str | None = None
    storefront: Storefront | None = None
    view: str | None = None


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
