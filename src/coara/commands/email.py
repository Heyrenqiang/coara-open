"""Email slash commands: /email (configure / status)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara

_USAGE = "用法: /email ｜ /email status ｜ /email <邮箱> <授权码> [imap] [smtp] [port]"


def _current() -> dict[str, str]:
    return {
        "email": os.getenv("COARA_EMAIL", ""),
        "imap": os.getenv("COARA_EMAIL_IMAP_SERVER", "imap.qq.com"),
        "smtp": os.getenv("COARA_EMAIL_SMTP_SERVER", "smtp.qq.com"),
        "port": os.getenv("COARA_EMAIL_SMTP_PORT", "465"),
    }


def _persist(email: str, pwd: str, imap: str, smtp: str, port: int) -> None:
    """写入 system/.env（COARA_EMAIL*）并使内存配置立即生效。"""
    from src.coara.frontend import get_frontend

    env = get_frontend().system_env_path()
    get_frontend().write_api_key(env, "COARA_EMAIL", email)
    get_frontend().write_api_key(env, "COARA_EMAIL_PASSWORD", pwd)
    get_frontend().write_api_key(env, "COARA_EMAIL_IMAP_SERVER", imap)
    get_frontend().write_api_key(env, "COARA_EMAIL_SMTP_SERVER", smtp)
    get_frontend().write_api_key(env, "COARA_EMAIL_SMTP_PORT", str(port))
    for k, v in (
        ("COARA_EMAIL", email),
        ("COARA_EMAIL_PASSWORD", pwd),
        ("COARA_EMAIL_IMAP_SERVER", imap),
        ("COARA_EMAIL_SMTP_SERVER", smtp),
        ("COARA_EMAIL_SMTP_PORT", str(port)),
    ):
        os.environ[k] = v
    from src.tools.builtin.email import email_client

    email_client.configure_email(email, pwd, imap, smtp, port)


def _configure(email: str, pwd: str, imap: str, smtp: str, port: int) -> CommandResult:
    _persist(email, pwd, imap, smtp, port)
    return CommandResult(
        f"邮箱已配置：{email}\nIMAP：{imap} ｜ SMTP：{smtp}:{port}\n"
        "已写入 system/.env，重启后仍可用。用 /email status 查看。"
    )


def _status() -> CommandResult:
    c = _current()
    if not c["email"]:
        return CommandResult("邮箱未配置。输入 /email 配置（填邮箱与授权码）。")
    return CommandResult(
        f"邮箱：{c['email']}\nIMAP：{c['imap']} ｜ SMTP：{c['smtp']}:{c['port']}\n"
        "用法：/email 重新配置 ｜ /email status 查看配置"
    )


@register("email")
async def handle_email(root: RootCoara, args: CommandArgs) -> CommandResult:
    if args.sub == "status":
        return _status()

    # 参数式：/email <邮箱> <授权码> [imap] [smtp] [port]
    if args.value and len(args.parts) >= 3:
        email = args.parts[1]
        pwd = args.parts[2]
        imap = args.parts[3] if len(args.parts) > 3 else "imap.qq.com"
        smtp = args.parts[4] if len(args.parts) > 4 else "smtp.qq.com"
        try:
            port = int(args.parts[5]) if len(args.parts) > 5 else 465
        except ValueError:
            port = 465
        return _configure(email, pwd, imap, smtp, port)

    # 交互向导：邮箱 → 授权码 → IMAP/SMTP/端口（含默认）
    if args.sub is None or args.sub in ("config", "setup"):
        try:
            import questionary
        except ImportError:
            return CommandResult.error("交互配置需要 questionary，请 pip install questionary")
        email = (await questionary.text("邮箱地址：").ask_async() or "").strip()
        if not email:
            return CommandResult("已取消", data={"cancelled": True})
        pwd = (await questionary.password("邮箱授权码（不是登录密码）：").ask_async() or "").strip()
        if not pwd:
            return CommandResult("已取消（未填授权码）", data={"cancelled": True})
        imap = (
            (await questionary.text("IMAP 服务器：", default="imap.qq.com").ask_async() or "imap.qq.com").strip()
            or "imap.qq.com"
        )
        smtp = (
            (await questionary.text("SMTP 服务器：", default="smtp.qq.com").ask_async() or "smtp.qq.com").strip()
            or "smtp.qq.com"
        )
        port_raw = (await questionary.text("SMTP 端口：", default="465").ask_async() or "465").strip() or "465"
        try:
            port = int(port_raw)
        except ValueError:
            port = 465
        return _configure(email, pwd, imap, smtp, port)

    return CommandResult(_USAGE, data={"usage": True})
