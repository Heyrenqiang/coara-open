"""CLI `@服务台` 投递：解析 / 名单 / 解析目标会话"""

from __future__ import annotations

import re
from typing import Any

from src.workspace.types import WorkspaceKind

# 整段消息必须是「@台名 + 可选正文」
_AT_DESK_RE = re.compile(r"^@(?P<name>\S+)(?:\s+(?P<body>[\s\S]*))?$")

# 配置助手别名 → 展示名
_CONFIG_ALIASES = frozenset({"配置", "config"})
_CONFIG_DISPLAY = "配置"

# 记录空间服务台别名：daily 与记录是同一会话的两个名字（persona=daily、条目名=记录）
_DAILY_ALIASES = frozenset({"daily", "记录", "records", "record"})
_DAILY_DISPLAY = "记录"


class ServiceDeskError(ValueError):
    """用户可见的服务台投递错误（中文）。"""


def parse_service_desk_at(text: str) -> tuple[str, str] | None:
    """若整段是行首 ``@name …``，返回 ``(name, body)``；否则 ``None``。"""
    raw = (text or "").strip()
    if not raw.startswith("@"):
        return None
    match = _AT_DESK_RE.match(raw)
    if match is None:
        return None
    name = str(match.group("name") or "").strip()
    if not name:
        return None
    body = str(match.group("body") or "").strip()
    return name, body


def _is_config_alias(name: str) -> bool:
    key = str(name or "").strip()
    return bool(key) and key.lower() in {a.lower() for a in _CONFIG_ALIASES}


def _is_daily_alias(name: str) -> bool:
    key = str(name or "").strip()
    return bool(key) and key.lower() in {a.lower() for a in _DAILY_ALIASES}


def canonical_desk_name(name: str) -> str:
    """补全/台签用的展示名。"""
    key = str(name or "").strip()
    if _is_config_alias(key):
        return _CONFIG_DISPLAY
    if _is_daily_alias(key):
        return _DAILY_DISPLAY
    return key


def list_service_desks(root: Any) -> list[dict[str, str]]:
    """握手/补全用名单：固定「配置」+ registry 内 CLI 可接的 internal 空间。"""
    desks: list[dict[str, str]] = [
        {"name": _CONFIG_DISPLAY, "summary": "系统配置助手"},
    ]
    seen = {_CONFIG_DISPLAY.lower(), "config"}
    wm = getattr(root, "workspace_manager", None)
    registry = getattr(wm, "registry", None) if wm is not None else None
    if registry is None:
        return desks
    try:
        entries = registry.list_active(include_internal=True)
    except Exception:
        return desks
    for entry in entries:
        if getattr(entry, "kind", None) != WorkspaceKind.INTERNAL:
            continue
        end_ok = getattr(entry, "end_allowed", None)
        if callable(end_ok) and not end_ok("cli"):
            continue
        name = str(getattr(entry, "name", "") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        desks.append(
            {
                "name": name,
                "summary": str(getattr(entry, "summary", "") or "").strip() or "系统服务空间",
            }
        )
    return desks


def is_known_desk_name(root: Any, name: str) -> bool:
    key = str(name or "").strip()
    if not key:
        return False
    if _is_config_alias(key):
        return True
    if _is_daily_alias(key):
        # 记录空间服务台是常驻目标（条目未登记时 resolve 阶段才报「未就绪」）
        return True
    display = canonical_desk_name(key)
    for desk in list_service_desks(root):
        if desk["name"] == key or desk["name"] == display:
            return True
        if desk["name"].lower() == key.lower():
            return True
    return False


async def resolve_service_desk_coara(root: Any, name: str, *, web_server: Any | None = None) -> Any:
    """解析服务台名 → 可 ``process_message`` 的 CoaraBase"""
    key = str(name or "").strip()
    if not key:
        raise ServiceDeskError("缺少服务台名")

    if _is_config_alias(key):
        server = web_server if web_server is not None else getattr(root, "_web_server", None)
        getter = getattr(server, "_get_module_root", None) if server is not None else None
        if getter is None:
            raise ServiceDeskError("配置助手当前不可用（Web 服务未就绪）")
        try:
            module_root = await getter("config")
        except Exception as exc:
            raise ServiceDeskError(f"配置助手未就绪：{exc}") from exc
        if module_root is None:
            raise ServiceDeskError("配置助手未就绪")
        return module_root

    # 合并后条目名=记录、persona=daily，两个名字都必须路由到同一专属会话
    if _is_daily_alias(key):
        from src.records.daily_curator import ensure_daily_workspace_session

        session = await ensure_daily_workspace_session(root)
        if session is None:
            raise ServiceDeskError("记录服务台未就绪")
        return session.coara

    wm = getattr(root, "workspace_manager", None)
    registry = getattr(wm, "registry", None) if wm is not None else None
    if registry is None:
        raise ServiceDeskError("工作空间未启用")

    entry = registry.resolve_name_or_id(key)
    if entry is None:
        raise ServiceDeskError(f"未知服务台 @{key}")
    if getattr(entry, "kind", None) != WorkspaceKind.INTERNAL:
        raise ServiceDeskError(f"@{entry.name} 不是服务空间（仅系统服务台可 @）")
    end_ok = getattr(entry, "end_allowed", None)
    if callable(end_ok) and not end_ok("cli"):
        raise ServiceDeskError(f"@{entry.name} 不能从 CLI 接入")

    # daily/记录 兜底：按 id/旧名解析到条目后，仍路由到记录空间专属会话
    if str(entry.name).strip().lower() in {a.lower() for a in _DAILY_ALIASES}:
        from src.records.daily_curator import ensure_daily_workspace_session

        session = await ensure_daily_workspace_session(root)
        if session is None:
            raise ServiceDeskError("记录服务台未就绪")
        return session.coara

    ensure = getattr(root, "ensure_workspace_session", None)
    if ensure is None:
        raise ServiceDeskError("无法打开服务台会话")
    session = await ensure(entry)
    return session.coara
