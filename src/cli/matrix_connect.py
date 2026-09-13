"""Shared Matrix / GoMatrix connection helpers for CLI frontends.

Typical flow (no forced rewrite of working config):
  1. User starts GoMatrix
  2. coara starts and connects to the configured homeserver (usually http://127.0.0.1:8008)
  3. Phone App scans Dashboard QR② (Cloudflare tunnel) to reach the same GoMatrix

If the configured URL is a docs placeholder / unreachable, fall back to local
GoMatrix discovery. Working configs are left alone.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import aiohttp

from src.cli.product_defaults import (
    MATRIX_BOT_USER,
    MATRIX_HOMESERVER,
    MATRIX_PORT,
    MATRIX_SERVER_NAME,
)
from src.core.types import MatrixConfig

DEFAULT_GOMAX_PORT = MATRIX_PORT

# Docs templates sometimes leave these as active values by mistake.
_PLACEHOLDER_HOMESERVER_MARKERS = (
    "your-url.trycloudflare.com",
    "xxxx.trycloudflare.com",
    "xxx.trycloudflare.com",
)


def is_placeholder_homeserver(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_HOMESERVER_MARKERS)


def prefers_local_homeserver_first(url: str) -> bool:
    """True for docs placeholders or phone-tunnel URLs mistakenly used as PC homeserver."""
    lowered = (url or "").strip().lower()
    if is_placeholder_homeserver(lowered):
        return True
    return "trycloudflare.com" in lowered


_PLACEHOLDER_MATRIX_PASSWORDS = frozenset(
    {
        "",
        "xxx",
        "changeme",
        "change-me",
        "agent-password",
        "agent-password-change-me",
        "your-password",
        "password",
    }
)


def is_placeholder_matrix_password(password: str) -> bool:
    return (password or "").strip().lower() in _PLACEHOLDER_MATRIX_PASSWORDS


def local_homeserver_url(port: int | None = None) -> str:
    if port is None or port == MATRIX_PORT:
        return MATRIX_HOMESERVER
    return f"http://127.0.0.1:{port}"


async def check_homeserver_reachable(homeserver_url: str) -> bool:
    """Return True when a Matrix homeserver responds at the given base URL."""
    url = homeserver_url.rstrip("/") + "/_matrix/client/versions"
    try:
        from src.matrix_client.http_session import shared_matrix_http_session

        session = shared_matrix_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception:
        return False


def build_matrix_settings(matrix_cfg: MatrixConfig | None) -> dict[str, Any]:
    """Resolve Matrix settings from env + config. Keep working values as-is."""
    server_name = (matrix_cfg.server_name if matrix_cfg else MATRIX_SERVER_NAME) or MATRIX_SERVER_NAME
    port = (matrix_cfg.port if matrix_cfg else DEFAULT_GOMAX_PORT) or DEFAULT_GOMAX_PORT

    homeserver = os.environ.get("COARA_MATRIX_HOMESERVER") or (
        matrix_cfg.homeserver if matrix_cfg and matrix_cfg.homeserver else ""
    )
    matrix_user = os.environ.get("COARA_MATRIX_USER") or (matrix_cfg.user if matrix_cfg and matrix_cfg.user else "")
    matrix_password = os.environ.get("COARA_MATRIX_PASSWORD") or (
        matrix_cfg.password if matrix_cfg and matrix_cfg.password else ""
    )
    notify_room_id = os.environ.get("COARA_MATRIX_NOTIFY_ROOM") or (
        matrix_cfg.notify_room_id if matrix_cfg and matrix_cfg.notify_room_id else ""
    )

    # Empty or docs placeholder → default local. Otherwise trust the configured URL.
    if not homeserver or is_placeholder_homeserver(homeserver):
        homeserver = local_homeserver_url(port)

    if not matrix_user:
        matrix_user = MATRIX_BOT_USER if server_name == MATRIX_SERVER_NAME else f"@coara:{server_name}"
    # 无默认密码：缺失时保持为空，由调用方决定是否报错（见 prepare_matrix_connection）

    return {
        "server_name": server_name,
        "port": port,
        "homeserver": homeserver,
        "user": matrix_user,
        "password": matrix_password,
        "notify_room_id": notify_room_id,
    }


def normalize_bot_credentials(user: str, password: str, server_name: str) -> tuple[str, str]:
    """Fill the GoMatrix default bot *user* when missing; password has no default
    (installer-generated, see deploy/release/install.*), so missing/placeholder
    passwords stay as-is and the caller decides whether to error."""
    trimmed = user.strip()
    default_user = f"@coara:{server_name}" if server_name else MATRIX_BOT_USER
    if not trimmed:
        return default_user, password
    localpart = trimmed.removeprefix("@").split(":", 1)[0]
    if is_placeholder_matrix_password(password) and (localpart == "coara" or not localpart):
        return default_user, password
    return user, password


def persist_matrix_bot_credentials(user: str, password: str) -> None:
    """Write COARA_MATRIX_USER / PASSWORD into system\\.env and current process env."""
    try:
        from src.cli.first_run_setup import system_env_path, write_api_key

        env_path = system_env_path()
        write_api_key(env_path, "COARA_MATRIX_USER", user)
        write_api_key(env_path, "COARA_MATRIX_PASSWORD", password)
    except Exception:
        pass
    os.environ["COARA_MATRIX_USER"] = user
    os.environ["COARA_MATRIX_PASSWORD"] = password


def find_gomatrix_exe() -> Path | None:
    """Locate bundled gomatrix next to the installed coara tree."""
    candidates: list[Path] = []
    root = os.environ.get("COARA_ROOT", "").strip()
    if root:
        candidates.append(Path(root) / "bin" / ("gomatrix.exe" if os.name == "nt" else "gomatrix"))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local and os.name == "nt":
        candidates.append(Path(local) / "coara" / "bin" / "gomatrix.exe")
    home = Path.home()
    candidates.append(home / ".local" / "coara" / "bin" / "gomatrix")
    for path in candidates:
        if path.is_file():
            return path
    return None


async def ensure_local_gomatrix(port: int) -> bool:
    """If local GoMatrix is down, try starting the bundled exe once. Non-destructive."""
    local = local_homeserver_url(port)
    if await check_homeserver_reachable(local):
        return True
    exe = find_gomatrix_exe()
    if exe is None:
        return False
    try:
        kwargs: dict[str, Any] = {
            "cwd": str(exe.parent),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
            kwargs["close_fds"] = True
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([str(exe)], **kwargs)
    except OSError:
        return False
    for _ in range(30):
        await asyncio.sleep(0.5)
        if await check_homeserver_reachable(local):
            return True
    return False


def persist_matrix_homeserver(url: str) -> None:
    """Optionally rewrite COARA_MATRIX_HOMESERVER after a successful fallback."""
    try:
        from src.cli.first_run_setup import system_env_path, write_api_key

        write_api_key(system_env_path(), "COARA_MATRIX_HOMESERVER", url)
        os.environ["COARA_MATRIX_HOMESERVER"] = url
    except Exception:
        os.environ["COARA_MATRIX_HOMESERVER"] = url


async def resolve_reachable_homeserver(homeserver: str, port: int) -> tuple[str | None, bool]:
    """Try configured URL first (unless placeholder/tunnel), then local GoMatrix."""
    normalized = (homeserver or "").rstrip("/")
    local = local_homeserver_url(port)
    default_local = local_homeserver_url(DEFAULT_GOMAX_PORT)
    prefer_local = prefers_local_homeserver_first(normalized) or is_placeholder_homeserver(normalized)

    candidates: list[str] = []
    if prefer_local:
        candidates.append(local)
        if default_local not in candidates:
            candidates.append(default_local)
        if normalized and not is_placeholder_homeserver(normalized) and normalized not in candidates:
            candidates.append(normalized)
    else:
        if normalized:
            candidates.append(normalized)
        if local not in candidates:
            candidates.append(local)
        if default_local not in candidates:
            candidates.append(default_local)

    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        key = c.rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(c)

    # If we will probe local anyway, give bundled gomatrix a chance to come up.
    if any(c.startswith("http://127.0.0.1:") or c.startswith("http://localhost:") for c in ordered):
        await ensure_local_gomatrix(port or DEFAULT_GOMAX_PORT)

    probed = await asyncio.gather(*(check_homeserver_reachable(c) for c in ordered))
    for candidate, ok in zip(ordered, probed, strict=True):
        if ok:
            migrated = candidate.rstrip("/") != normalized.rstrip("/")
            return candidate, migrated
    return None, False


def matrix_config_incomplete(settings: dict[str, Any]) -> bool:
    return not settings.get("homeserver") or not settings.get("user") or not settings.get("password")


async def prepare_matrix_connection(
    matrix_cfg: MatrixConfig | None,
    *,
    coara_home: Path | None = None,
) -> tuple[bool, dict[str, Any] | None, list[str]]:
    """Connect using configured homeserver; fall back to local GoMatrix when needed."""
    if coara_home is not None:
        from src.matrix_host.credentials import ensure_matrix_bot_credentials, load_matrix_password_from_data_dir

        port = int(getattr(matrix_cfg, "port", DEFAULT_GOMAX_PORT) or DEFAULT_GOMAX_PORT)
        ensure_matrix_bot_credentials(coara_home, port=port)
        toml_password = load_matrix_password_from_data_dir(coara_home)
        if toml_password:
            os.environ["COARA_MATRIX_PASSWORD"] = toml_password

    settings = build_matrix_settings(matrix_cfg)
    if matrix_config_incomplete(settings):
        missing = [
            label
            for label, value in (
                ("homeserver", settings.get("homeserver")),
                ("user", settings.get("user")),
                ("password", settings.get("password")),
            )
            if not value
        ]
        return (
            False,
            None,
            [
                "Matrix 配置不完整（缺少 " + "、".join(missing) + "），无法连接。",
                "请在 system\\.env 配置 COARA_MATRIX_HOMESERVER / COARA_MATRIX_USER / COARA_MATRIX_PASSWORD。",
            ],
        )

    configured = settings["homeserver"]
    port = int(settings["port"]) or DEFAULT_GOMAX_PORT
    reachable, migrated = await resolve_reachable_homeserver(configured, port)
    if reachable is None:
        return (
            False,
            None,
            [
                f"GoMatrix 未在 {configured} 运行。",
                "请先启动 GoMatrix（桌面「GoMatrix」，或 %LOCALAPPDATA%\\coara\\bin\\gomatrix.exe），再开 coara。",
                "也可以直接开 coara：若本机 GoMatrix 未运行，会尝试自动拉起。",
                f"确认 COARA_MATRIX_HOMESERVER 指向本机（例如 {local_homeserver_url(port)}）。",
                "手机 App 通过 Dashboard QR②（Cloudflare）连接同一台 GoMatrix。",
            ],
        )

    user = settings["user"]
    password = settings["password"]
    warnings: list[str] = []
    notify_room_id = settings["notify_room_id"]

    # Heal missing / placeholder bot creds even when the homeserver URL is
    # already local (common after zip reinstall wiped the GoMatrix DB while
    # .env still had an old password).
    healed_user, healed_password = normalize_bot_credentials(user, password, settings["server_name"])
    creds_changed = healed_user != user or healed_password != password
    user, password = healed_user, healed_password

    # 无默认密码：缺失/占位符密码直接报错，绝不写回 .env（避免污染真实凭据）
    if not password or is_placeholder_matrix_password(password):
        return (
            False,
            None,
            [
                "Matrix Bot 密码缺失或为占位符，无法连接。",
                "请在 system\\.env 配置 COARA_MATRIX_PASSWORD（与 gomatrix.toml 中 coara bot 密码一致），",
                "或删除旧凭据后重新运行安装器生成随机密码。",
            ],
        )

    if migrated:
        warnings.append(f"[Matrix] 配置地址 {configured} 不可用，已自动连到 {reachable}。")
        # Only rewrite .env when falling back from a bad placeholder/tunnel URL.
        if prefers_local_homeserver_first(configured) or is_placeholder_homeserver(configured):
            persist_matrix_homeserver(reachable)
        if notify_room_id:
            notify_room_id = ""
            warnings.append("[Matrix] 已忽略旧通知房间；手机扫 QR② 进房即可。")

    if creds_changed:
        persist_matrix_bot_credentials(user, password)
        warnings.append(f"[Matrix] Bot 账号已对齐为 {user}（GoMatrix 默认）。已写入 system\\.env。")

    return (
        True,
        {
            "homeserver": reachable,
            "user": user,
            "password": password,
            "notify_room_id": notify_room_id,
        },
        warnings,
    )
