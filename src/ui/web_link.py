"""Web 配置页链接解析：host/port、token、URL 组装。

零重依赖（不引 aiohttp），CLI 首启向导、attach 客户端等早期路径共用。
所有函数均为 best-effort：任何一步失败抛给调用方决定降级。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from src.core.coara_home import resolve_bootstrap_coara_home, resolve_coara_home, user_dir_for_home
from src.ui.dashboard_tokens import load_or_create_dashboard_token

DEFAULT_WEB_PORT = 8080


def resolve_web_host_port() -> tuple[str, int]:
    raw = os.environ.get("COARA_WEB_PORT", str(DEFAULT_WEB_PORT)).strip() or str(DEFAULT_WEB_PORT)
    try:
        port = int(raw)
    except ValueError:
        port = DEFAULT_WEB_PORT
    return "127.0.0.1", port


def _configured_home_from_yaml(workspace: Path) -> Path | None:
    """Read ``coara_home`` from user config without bootstrapping config_manager."""
    candidates: list[Path] = []
    bootstrap = resolve_bootstrap_coara_home()
    if bootstrap is not None:
        candidates.append(user_dir_for_home(bootstrap) / "config.yaml")
    candidates.append(workspace / ".coara" / "users" / "default" / "config.yaml")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            raw = data.get("coara_home")
            if raw:
                home = Path(str(raw)).expanduser()
                if home.is_dir():
                    return home.resolve()
    return None


def resolve_web_coara_home(workspace: Path) -> Path:
    configured = _configured_home_from_yaml(workspace)
    if configured is not None:
        return configured
    return resolve_coara_home(workspace)


def load_web_token(workspace: Path, coara_home: Path | None = None) -> str:
    home = coara_home or resolve_web_coara_home(workspace)
    return load_or_create_dashboard_token(workspace, home)


def build_web_url(workspace: Path, path: str = "") -> str | None:
    """组装 ``http://127.0.0.1:{port}{path}?token=...``；token 不可得返回 None。"""
    try:
        token = load_web_token(workspace)
    except Exception:
        return None
    if not token:
        return None
    host, port = resolve_web_host_port()
    return f"http://{host}:{port}{path}?token={token}"
