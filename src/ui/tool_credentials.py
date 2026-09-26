"""配置页为需要凭据的工具填写 key：只写 system/.env，不把密钥回传前端。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.core.errors import ConfigError


@dataclass(frozen=True, slots=True)
class CredentialField:
    id: str
    label: str
    secret: bool


CREDENTIAL_TOOLS: dict[str, tuple[CredentialField, ...]] = {
    "web_search": (
        CredentialField("EXA_API_KEY", "Exa", True),
        CredentialField("SERPER_API_KEY", "Serper", True),
        CredentialField("LINKUP_API_KEY", "Linkup", True),
        CredentialField("DOUBAO_API_KEY", "豆包", True),
    ),
    "email": (
        CredentialField("COARA_EMAIL", "邮箱", False),
        CredentialField("COARA_EMAIL_PASSWORD", "授权码", True),
    ),
    "media": (CredentialField("AGNES_MEDIA_API_KEY", "Agnes 媒体", True),),
}

_ALLOWED_IDS = {field.id for fields in CREDENTIAL_TOOLS.values() for field in fields}


def credential_fields(name: str) -> list[dict[str, object]]:
    """给前端的字段状态。密钥只报有没有，明文只回非机密项（如邮箱地址）。"""
    fields = CREDENTIAL_TOOLS.get(name)
    if fields is None:
        return []
    rows: list[dict[str, object]] = []
    for field in fields:
        current = os.environ.get(field.id, "").strip()
        row: dict[str, object] = {"id": field.id, "label": field.label, "secret": field.secret}
        if field.secret:
            row["set"] = bool(current)
        else:
            row["value"] = current
        rows.append(row)
    return rows


def save_tool_credentials(name: str, values: dict[str, str], *, root: Any | None = None) -> list[str]:
    """把非空字段写入 system/.env 并同步进本进程环境。返回实际写入的变量名。"""
    fields = CREDENTIAL_TOOLS.get(name)
    if fields is None:
        raise ConfigError(f"工具 {name} 没有可填写的凭据")
    allowed = {field.id for field in fields}
    unknown = [key for key in values if key not in allowed or key not in _ALLOWED_IDS]
    if unknown:
        raise ConfigError("含有不允许写入的字段")
    pending = {key: str(values.get(key) or "").strip() for key in allowed}
    pending = {key: value for key, value in pending.items() if value}
    if not pending:
        raise ConfigError("请填写要保存的内容")

    from src.core.api_keys import system_env_path, write_api_key

    env_path = system_env_path()
    written: list[str] = []
    for key, value in pending.items():
        write_api_key(env_path, key, value)
        os.environ[key] = value
        written.append(key)
    _apply_side_effects(name, root)
    return written


def _smtp_port() -> int:
    raw = os.environ.get("COARA_EMAIL_SMTP_PORT", "465").strip() or "465"
    try:
        return int(raw)
    except ValueError:
        return 465


def _apply_side_effects(name: str, root: Any | None = None) -> None:
    if name == "email":
        email = os.environ.get("COARA_EMAIL", "").strip()
        password = os.environ.get("COARA_EMAIL_PASSWORD", "").strip()
        if email and password:
            from src.tools.builtin.email.email_client import configure_email

            configure_email(
                email,
                password,
                os.environ.get("COARA_EMAIL_IMAP_SERVER", "imap.qq.com").strip() or "imap.qq.com",
                os.environ.get("COARA_EMAIL_SMTP_SERVER", "smtp.qq.com").strip() or "smtp.qq.com",
                _smtp_port(),
            )
    elif name == "media":
        from src.coara.runtime_tools import register_media_if_configured

        register_media_if_configured(root)
