"""配置页「工具」tab 的 /api/v1/tools payload 构造 模块级纯函数便于不依赖真实内核的轻量测试：root 为 None（无前台会话）
时 退回进程级全局注册表，仍列出已注册工具"""

from __future__ import annotations

import os
from typing import Any

from src.core.logger import logger
from src.core.tool_base import BaseTool

_SEARCH_KEY_VARS = ("EXA_API_KEY", "SERPER_API_KEY", "LINKUP_API_KEY", "DOUBAO_API_KEY")


def _collect_tools(root: Any | None) -> dict[str, BaseTool]:
    """以 root 前台会话的工具为准，全局注册表兜底合并"""
    tools: dict[str, BaseTool] = {}
    try:
        from src.tools.registry import tool_registry

        for tool in tool_registry.list_all():
            tools.setdefault(tool.name, tool)
    except Exception as exc:
        logger.debug(f"tools payload: global registry unavailable: {exc}")
    if root is not None:
        try:
            manager = getattr(root.foreground_coara, "_tool_manager", None)
            session_tools = getattr(manager, "tools", None)
            if isinstance(session_tools, dict):
                for name, tool in session_tools.items():
                    if isinstance(tool, BaseTool):
                        tools[name] = tool
        except Exception as exc:
            logger.debug(f"tools payload: foreground tools unavailable: {exc}")
    return tools


def _short_description(tool: BaseTool) -> str:
    summary = (getattr(tool, "summary", "") or "").strip()
    if summary:
        return summary.splitlines()[0].strip()
    description = (getattr(tool, "description", "") or "").strip()
    return description.splitlines()[0].strip() if description else ""


def _email_status() -> tuple[str | None, str | None]:
    try:
        from src.tools.builtin.email import email_client

        if email_client._email_config:
            return "ready", None
    except Exception as exc:
        logger.debug(f"tools payload: email status check failed: {exc}")
    hint = "需配置 SMTP/IMAP 账号与授权码（环境变量 COARA_EMAIL / COARA_EMAIL_PASSWORD），找配置助手帮忙"
    return "needs_config", hint


def _web_search_status() -> tuple[str | None, str | None]:
    # baidu 免密可用，全空不算缺失，只提示可升级
    if any(os.getenv(name, "").strip() for name in _SEARCH_KEY_VARS):
        return "ready", None
    return None, "配置 EXA/SERPER 等厂商 key 可获得更好搜索质量"


def _media_status() -> tuple[str | None, str | None]:
    try:
        from src.tools.builtin.media.providers import agnes_key

        agnes_key()
        return "ready", None
    except Exception:
        return (
            "needs_config",
            "需在配置页「工具」填写 AGNES_MEDIA_API_KEY，找配置助手帮忙",
        )


_STATUS_CHECKS = {
    "email": _email_status,
    "web_search": _web_search_status,
    "media": _media_status,
}


def build_tools_payload(root: Any | None) -> dict[str, Any]:
    """构造 /api/v1/tools 响应体

    status 仅对需要凭据的工具给出：ready / needs_config；其余一律 None（开箱即用不打扰）
    """
    tools = _collect_tools(root)
    rows: list[dict[str, Any]] = []
    for name in sorted(tools):
        tool = tools[name]
        status: str | None = None
        hint: str | None = None
        check = _STATUS_CHECKS.get(name)
        if check is not None:
            try:
                status, hint = check()
            except Exception as exc:
                logger.debug(f"tools payload: status check for {name} failed: {exc}")
        rows.append(
            {
                "name": name,
                "category": (getattr(tool, "category", "") or "general"),
                "description": _short_description(tool),
                "deferred": bool(getattr(tool, "should_defer", False)),
                "status": status,
                "config_hint": hint,
            }
        )
    _attach_credentials(rows)
    return {"tools": rows, "count": len(rows)}


def _attach_credentials(rows: list[dict[str, Any]]) -> None:
    """web_search / email / media 带上可填写的凭据。没注册时也留一行，否则填不了 key。"""
    from src.ui.tool_credentials import CREDENTIAL_TOOLS, credential_fields

    fallback = {
        "web_search": ("web", "网页搜索"),
        "email": ("communication", "收发邮件"),
        "media": ("media", "文生图 / 图生视频"),
    }
    by_name = {str(row["name"]): row for row in rows}
    for name in CREDENTIAL_TOOLS:
        fields = credential_fields(name)
        row = by_name.get(name)
        if row is not None:
            row["credentials"] = fields
            continue
        category, description = fallback[name]
        rows.append(
            {
                "name": name,
                "category": category,
                "description": description,
                "deferred": name == "media",
                "status": "needs_config",
                "config_hint": None,
                "credentials": fields,
            }
        )
