"""邮件工具（内置挂起，tool(activate) 揭示后可用）。

能力源自退役的 MCP email server，语义保持一致：IMAP 收 + SMTP 发，
配置走进程内存 + COARA_EMAIL* 环境变量自动加载。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

from . import email_client

_COMMON_NOTE = "系统启动时已从环境变量自动加载邮箱配置（COARA_EMAIL 等），通常可直接用，无需 configure。"

# action → (目标函数, 展示标签, 透传参数白名单)
_ACTIONS: dict[str, tuple[Callable[..., Any], str, tuple[str, ...]]] = {
    "configure": (
        email_client.configure_email,
        "配置邮箱",
        ("email", "password", "imap_server", "smtp_server", "smtp_port"),
    ),
    "list_folders": (email_client.list_email_folders, "列出邮件文件夹", ()),
    "list": (email_client.list_emails, "查看邮件列表", ("folder", "limit")),
    "search": (
        email_client.search_emails,
        "搜索邮件",
        ("folder", "query", "from_addr", "subject", "unread_only", "limit"),
    ),
    "latest": (email_client.get_latest_email_detail, "查看最新邮件", ("folder",)),
    "detail": (email_client.get_email_detail, "查看邮件详情", ("uid", "folder")),
    "mark_read": (email_client.mark_email_read, "标记邮件已读", ("uid", "folder")),
    "delete": (email_client.delete_email, "删除邮件", ("uid", "folder")),
    "send": (email_client.send_email, "发送邮件", ("to", "subject", "body", "attachments")),
}


class _Invocation(ToolInvocation):
    def get_description(self) -> str:
        action = str(self.params.get("action") or "")
        return _ACTIONS.get(action, (None, "邮件", ()))[1]

    async def execute(self, signal=None) -> ToolResult:
        action = str(self.params.get("action") or "")
        entry = _ACTIONS.get(action)
        if entry is None:
            return ToolResult.error(f"未知 action：{action!r}，可用 {sorted(_ACTIONS)}")
        fn, _, keys = entry
        kwargs = {k: self.params[k] for k in keys if k in self.params}
        try:
            result = await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            return ToolResult.error(str(exc))
        if isinstance(result, str):
            return ToolResult.success(result)
        return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))


class EmailTool(BaseTool):
    name = "email"
    summary = "邮件收发，IMAP 收 + SMTP 发，纯文本正文，支持附件"
    description = (
        "邮件收发，IMAP 收 + SMTP 发，纯文本正文，支持附件。"
        + _COMMON_NOTE
        + "\n\n| action | 用途 |\n|--------|------|\n"
        "| `list` | 查看指定文件夹的最新邮件列表，每条含 uid 字段 |\n"
        "| `latest` | 获取文件夹中最新一封邮件的完整详情，无需 uid；"
        "用户说「最新的」「最近一封」时优先用，不要猜 uid |\n"
        "| `detail` | 获取单封邮件完整详情，含正文、附件列表；"
        "uid 必须来自 list/search 返回的 uid 字段（字符串），不是列表行号 |\n"
        "| `search` | 搜索邮件，支持正文关键词、发件人、主题、未读筛选 |\n"
        "| `list_folders` | 列出所有邮件文件夹，含收件箱、已发送、草稿、垃圾邮件等 |\n"
        "| `mark_read` | 将指定邮件标记为已读 |\n"
        "| `delete` | 删除指定邮件，移动到垃圾箱，可恢复 |\n"
        "| `send` | 发送邮件，需 to/subject/body，attachments 可选，走审批 |\n"
        "| `configure` | 配置邮箱账号，进程内存，不写磁盘；仅切换账号时用。"
        "常见配置，QQ imap.qq.com/smtp.qq.com/465；163 imap.163.com/smtp.163.com/465；"
        "Gmail imap.gmail.com/smtp.gmail.com/587 |"
    )
    display_name = "Email"
    category = "communication"
    kind = ToolKind.EXECUTE
    should_defer = True
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "要执行的邮件操作",
            },
            "folder": {"type": "string", "description": "邮件文件夹，默认 INBOX 收件箱", "default": "INBOX"},
            "limit": {"type": "integer", "description": "list/search 的返回数量上限", "default": 10},
            "uid": {"type": "string", "description": "detail/mark_read/delete 的邮件唯一标识（list 返回的 uid 字段）"},
            "query": {"type": "string", "description": "search 的正文关键词", "default": ""},
            "from_addr": {"type": "string", "description": "search 的发件人地址", "default": ""},
            "unread_only": {"type": "boolean", "description": "search 只返回未读邮件", "default": False},
            "to": {"type": "string", "description": "send 的收件人地址"},
            "subject": {"type": "string", "description": "send 的主题；search 的主题关键词"},
            "body": {"type": "string", "description": "send 的正文，纯文本"},
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "send 的附件文件绝对路径列表，可选",
            },
            "email": {"type": "string", "description": "configure 的邮箱地址"},
            "password": {"type": "string", "description": "configure 的邮箱授权码，不是登录密码"},
            "imap_server": {"type": "string", "description": "configure 的 IMAP 服务器地址", "default": "imap.qq.com"},
            "smtp_server": {"type": "string", "description": "configure 的 SMTP 服务器地址", "default": "smtp.qq.com"},
            "smtp_port": {
                "type": "integer",
                "description": "configure 的 SMTP 端口，QQ/163/126 通常 465",
                "default": 465,
            },
        },
        "required": ["action"],
    }

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:
        # 对外发送，发送前与用户确认一次
        return args.get("action") == "send"

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _Invocation(params)


EMAIL_TOOL_TYPES = (EmailTool,)
