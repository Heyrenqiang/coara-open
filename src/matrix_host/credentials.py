"""Keep gomatrix.toml [[agents]] coara bot credentials aligned with system/.env."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from src.cli.matrix_connect import is_placeholder_matrix_password
from src.cli.product_defaults import MATRIX_BOT_USER
from src.core.json_store import write_text_atomic
from src.core.logger import logger
from src.matrix_host.supervisor import _escape_toml_string

_AGENT_NAME = "coara"


def coara_agent_password_from_toml(text: str) -> str:
    """Return coara agent password from gomatrix.toml text, or empty string."""
    for block in re.split(r"(?m)^\[\[agents\]\]\s*$", text)[1:]:
        if not re.search(rf'(?m)^\s*name\s*=\s*"{re.escape(_AGENT_NAME)}"\s*$', block):
            continue
        match = re.search(r'(?m)^\s*password\s*=\s*"([^"]*)"', block)
        if match:
            return match.group(1)
    return ""


def upsert_coara_agent_in_toml(text: str, password: str) -> str:
    """Insert or update the coara [[agents]] block."""
    escaped = _escape_toml_string(password)
    if coara_agent_password_from_toml(text):
        return re.sub(
            rf'(?ms)(\[\[agents\]\][^\[]*?name\s*=\s*"{re.escape(_AGENT_NAME)}"[^\[]*?password\s*=\s*")[^"]*(")',
            rf"\g<1>{escaped}\g<2>",
            text,
            count=1,
        )
    block = (
        f'\n[[agents]]\nname = "{_AGENT_NAME}"\n'
        f'password = "{escaped}"\ndisplay_name = "coara"\ndefault = true\n'
    )
    return text.rstrip("\n") + block


def _env_file(coara_home: Path) -> Path:
    return coara_home / "system" / ".env"


def _read_env_password(coara_home: Path) -> str:
    env_path = _env_file(coara_home)
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("COARA_MATRIX_PASSWORD="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if value and not is_placeholder_matrix_password(value):
                    return value
    env_password = (os.environ.get("COARA_MATRIX_PASSWORD") or "").strip()
    if env_password and not is_placeholder_matrix_password(env_password):
        return env_password
    return ""


def _persist_credentials(coara_home: Path, user: str, password: str) -> None:
    from src.cli.first_run_setup import write_api_key

    env_path = _env_file(coara_home)
    write_api_key(env_path, "COARA_MATRIX_USER", user)
    write_api_key(env_path, "COARA_MATRIX_PASSWORD", password)
    os.environ["COARA_MATRIX_USER"] = user
    os.environ["COARA_MATRIX_PASSWORD"] = password


def ensure_matrix_bot_credentials(coara_home: Path, *, port: int = 8008) -> str:
    """Ensure ``<coara_home>/matrix/gomatrix.toml`` has coara agent + matching .env.

    gomatrix resets agent passwords from toml on every startup, so toml is the
    source of truth when both exist but disagree.
    """
    data_dir = coara_home / "matrix"
    data_dir.mkdir(parents=True, exist_ok=True)
    toml = data_dir / "gomatrix.toml"
    if not toml.is_file():
        from src.matrix_host.supervisor import _DEFAULT_TOML, _rewrite_port

        write_text_atomic(toml, _rewrite_port(_DEFAULT_TOML.format(port=port), port))

    try:
        text = toml.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"matrix host: gomatrix.toml 读取失败（{exc}），按空配置重建凭据块")
        text = ""
    toml_password = coara_agent_password_from_toml(text)
    env_password = _read_env_password(coara_home)

    if toml_password and env_password and toml_password != env_password:
        logger.warning("matrix host: .env 与 gomatrix.toml 的 coara 密码不一致，以 toml 为准同步 .env")
        _persist_credentials(coara_home, MATRIX_BOT_USER, toml_password)
        return toml_password

    if toml_password:
        if env_password != toml_password:
            _persist_credentials(coara_home, MATRIX_BOT_USER, toml_password)
        return toml_password

    password = env_password or secrets.token_urlsafe(32)
    write_text_atomic(toml, upsert_coara_agent_in_toml(text, password))
    _persist_credentials(coara_home, MATRIX_BOT_USER, password)
    logger.info("matrix host: 已写入 gomatrix.toml [[agents]] coara 并同步 .env")
    return password


def load_matrix_password_from_data_dir(coara_home: Path) -> str:
    """Read coara bot password from ``<coara_home>/matrix/gomatrix.toml`` if present."""
    toml = coara_home / "matrix" / "gomatrix.toml"
    if not toml.is_file():
        return ""
    try:
        return coara_agent_password_from_toml(toml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""
