"""邮件核心 — IMAP 收信 + SMTP 发信。

从 mcp-tools/email_client.py 迁入（MCP 退役，能力转内置挂起工具）。
配置保存在进程内存，不写磁盘；启动时从环境变量 COARA_EMAIL* 自动加载。
"""

from __future__ import annotations

import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

_LIST_BODY_PREVIEW_CHARS = 500
_DETAIL_TEXT_MAX_CHARS = 32_000
_DETAIL_HTML_MAX_CHARS = 32_000
# imap_tools defaults to US-ASCII; Chinese subject/body search raises UnicodeEncodeError.
_IMAP_CHARSET = "UTF-8"
_KNOWN_FOLDER_NAMES = frozenset(
    {"INBOX", "SENT", "DRAFTS", "TRASH", "JUNK", "SPAM", "ARCHIVE", "已发送", "草稿", "垃圾箱"}
)

# 运行时内存配置，不写入磁盘
_email_config: dict[str, Any] = {}

# 自动从环境变量加载默认配置（避免每次手动 configure_email）
if os.getenv("COARA_EMAIL") and os.getenv("COARA_EMAIL_PASSWORD"):
    _email_config = {
        "email": os.getenv("COARA_EMAIL", ""),
        "password": os.getenv("COARA_EMAIL_PASSWORD", ""),
        "imap_server": os.getenv("COARA_EMAIL_IMAP_SERVER", "imap.qq.com"),
        "smtp_server": os.getenv("COARA_EMAIL_SMTP_SERVER", "smtp.qq.com"),
        "smtp_port": int(os.getenv("COARA_EMAIL_SMTP_PORT", "465")),
    }


def _imap_tools():
    try:
        from imap_tools import AND, MailBox
    except ImportError as exc:
        raise RuntimeError("邮件功能需要 imap-tools 库：pip install imap-tools") from exc
    return AND, MailBox


def configure_email(
    email: str,
    password: str,
    imap_server: str = "imap.qq.com",
    smtp_server: str = "smtp.qq.com",
    smtp_port: int = 465,
) -> str:
    """配置邮箱账号信息（保存在内存中，不写入磁盘）。

    注意：模块导入时已自动从环境变量加载配置（COARA_EMAIL 等），
    通常不需要手动调用此函数。
    """
    _email_config["email"] = email
    _email_config["password"] = password
    _email_config["imap_server"] = imap_server
    _email_config["smtp_server"] = smtp_server
    _email_config["smtp_port"] = smtp_port
    return f"邮箱 {email} 配置成功"


def _ensure_configured() -> None:
    if not _email_config:
        raise RuntimeError("邮箱未配置。输入 /email 配置邮箱（填邮箱与授权码）。")


def _truncate_field(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n…(已截断，原文共 {len(value)} 字符)"


def _message_summary(msg: Any) -> dict[str, Any]:
    return {
        "uid": msg.uid,
        "subject": msg.subject,
        "from": msg.from_,
        "to": msg.to,
        "date": msg.date_str,
        "body": _truncate_field(msg.text or "", _LIST_BODY_PREVIEW_CHARS),
    }


def _message_detail(msg: Any) -> dict[str, Any]:
    attachments = [
        {
            "filename": att.filename,
            "content_type": att.content_type,
            "size": len(att.payload),
        }
        for att in msg.attachments
    ]
    return {
        "uid": msg.uid,
        "subject": msg.subject,
        "from": msg.from_,
        "to": msg.to,
        "date": msg.date_str,
        "text": _truncate_field(msg.text or "", _DETAIL_TEXT_MAX_CHARS),
        "html": _truncate_field(msg.html or "", _DETAIL_HTML_MAX_CHARS),
        "attachments": attachments,
    }


def _looks_like_folder_name(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    if normalized.upper() in _KNOWN_FOLDER_NAMES:
        return True
    return normalized in _KNOWN_FOLDER_NAMES


def list_emails(folder: str = "INBOX", limit: int = 5) -> list[dict[str, Any]]:
    """查看指定文件夹的最新邮件列表。"""
    _ensure_configured()
    _, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    emails: list[dict[str, Any]] = []
    with MailBox(_email_config["imap_server"]).login(
        _email_config["email"], _email_config["password"], initial_folder=folder
    ) as mailbox:
        for msg in mailbox.fetch(limit=limit, reverse=True, charset=_IMAP_CHARSET):
            emails.append(_message_summary(msg))
    return emails


def search_emails(
    folder: str = "INBOX",
    query: str = "",
    from_addr: str = "",
    subject: str = "",
    unread_only: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """搜索邮件（支持关键词、发件人、主题、未读筛选）。"""
    _ensure_configured()
    AND, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    criteria_kwargs: dict[str, Any] = {}
    if unread_only:
        criteria_kwargs["seen"] = False
    if from_addr:
        criteria_kwargs["from_"] = from_addr
    if subject:
        criteria_kwargs["subject"] = subject
    if query:
        criteria_kwargs["body"] = query

    emails: list[dict[str, Any]] = []
    with MailBox(_email_config["imap_server"]).login(
        _email_config["email"], _email_config["password"], initial_folder=folder
    ) as mailbox:
        fetch_kwargs = {"limit": limit, "reverse": True, "charset": _IMAP_CHARSET}
        if criteria_kwargs:
            msgs = mailbox.fetch(AND(**criteria_kwargs), **fetch_kwargs)
        else:
            msgs = mailbox.fetch(**fetch_kwargs)
        for msg in msgs:
            emails.append(_message_summary(msg))
    return emails


def get_latest_email_detail(folder: str = "INBOX") -> dict[str, Any]:
    """获取文件夹中最新一封邮件的完整详情（无需 uid）。"""
    _ensure_configured()
    _, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    with MailBox(_email_config["imap_server"]).login(
        _email_config["email"], _email_config["password"], initial_folder=folder
    ) as mailbox:
        for msg in mailbox.fetch(limit=1, reverse=True, charset=_IMAP_CHARSET):
            return _message_detail(msg)
    return {}


def get_email_detail(uid: str, folder: str = "INBOX") -> dict[str, Any]:
    """获取单封邮件的完整详情（HTML正文、附件列表）。"""
    _ensure_configured()
    AND, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    uid_str = str(uid).strip()
    if _looks_like_folder_name(uid_str):
        return get_latest_email_detail(folder=uid_str)

    with MailBox(_email_config["imap_server"]).login(
        _email_config["email"], _email_config["password"], initial_folder=folder
    ) as mailbox:
        for msg in mailbox.fetch(AND(uid=uid_str), charset=_IMAP_CHARSET):
            return _message_detail(msg)
    return {}


def list_email_folders() -> list[str]:
    """列出所有邮件文件夹（收件箱、已发送、草稿、垃圾邮件等）。"""
    _ensure_configured()
    _, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    with MailBox(_email_config["imap_server"]).login(_email_config["email"], _email_config["password"]) as mailbox:
        return [f.name for f in mailbox.folder.list()]


def mark_email_read(uid: str, folder: str = "INBOX") -> str:
    """将指定邮件标记为已读。"""
    _ensure_configured()
    _, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    with MailBox(_email_config["imap_server"]).login(
        _email_config["email"], _email_config["password"], initial_folder=folder
    ) as mailbox:
        mailbox.flag([uid], ["Seen"], True)
    return "已标记为已读"


def delete_email(uid: str, folder: str = "INBOX") -> str:
    """删除指定邮件（移动到垃圾箱）。"""
    _ensure_configured()
    _, MailBox = _imap_tools()  # noqa: N806  # 镜像 imaplib 命名
    with MailBox(_email_config["imap_server"]).login(
        _email_config["email"], _email_config["password"], initial_folder=folder
    ) as mailbox:
        mailbox.move([uid], "Trash")
    return "已移动到垃圾箱"


def send_email(
    to: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> str:
    """发送邮件，支持附件。"""
    _ensure_configured()
    msg = MIMEMultipart()
    msg["From"] = _email_config["email"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    attachments = attachments or []
    for path in attachments:
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = os.path.basename(path)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
            msg.attach(part)

    server = smtplib.SMTP_SSL(_email_config["smtp_server"], _email_config["smtp_port"])
    server.login(_email_config["email"], _email_config["password"])
    server.send_message(msg)
    server.quit()
    return "邮件发送成功"
