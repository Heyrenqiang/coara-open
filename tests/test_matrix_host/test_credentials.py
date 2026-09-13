"""Tests for gomatrix.toml / .env credential alignment."""

from __future__ import annotations

from pathlib import Path

from src.matrix_host.credentials import (
    coara_agent_password_from_toml,
    ensure_matrix_bot_credentials,
    upsert_coara_agent_in_toml,
)


def test_coara_agent_password_from_toml():
    text = """
server_name = "coara.local"

[[agents]]
name = "coara"
password = "secret123"
"""
    assert coara_agent_password_from_toml(text) == "secret123"


def test_upsert_coara_agent_in_toml_inserts_block():
    text = 'server_name = "coara.local"\n'
    updated = upsert_coara_agent_in_toml(text, "pw")
    assert 'password = "pw"' in updated
    assert coara_agent_password_from_toml(updated) == "pw"


def test_ensure_matrix_bot_credentials_syncs_env_from_toml(tmp_path: Path, monkeypatch):
    home = tmp_path / "coara"
    matrix_dir = home / "matrix"
    matrix_dir.mkdir(parents=True)
    toml = matrix_dir / "gomatrix.toml"
    toml.write_text(
        """
server_name = "coara.local"
port = 8008

[[agents]]
name = "coara"
password = "from-toml"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    system = home / "system"
    system.mkdir()
    (system / ".env").write_text("COARA_MATRIX_PASSWORD=old-env\n", encoding="utf-8")

    monkeypatch.setenv("COARA_HOME", str(home))
    password = ensure_matrix_bot_credentials(home, port=8008)
    assert password == "from-toml"
    env_text = (system / ".env").read_text(encoding="utf-8")
    assert "COARA_MATRIX_PASSWORD=from-toml" in env_text


def test_ensure_matrix_bot_credentials_writes_agent_when_missing(tmp_path: Path, monkeypatch):
    home = tmp_path / "coara"
    matrix_dir = home / "matrix"
    matrix_dir.mkdir(parents=True)
    toml = matrix_dir / "gomatrix.toml"
    toml.write_text('server_name = "coara.local"\nport = 8008\n', encoding="utf-8")
    system = home / "system"
    system.mkdir()
    (system / ".env").write_text("COARA_MATRIX_PASSWORD=env-only\n", encoding="utf-8")

    monkeypatch.setenv("COARA_HOME", str(home))
    password = ensure_matrix_bot_credentials(home, port=8008)
    assert password == "env-only"
    assert coara_agent_password_from_toml(toml.read_text(encoding="utf-8")) == "env-only"
