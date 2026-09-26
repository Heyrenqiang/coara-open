"""Tests for GoMatrix auto-discovery (no forced rewrite of working config)."""

from __future__ import annotations

import pytest

from src.cli.matrix_connect import (
    build_matrix_settings,
    is_placeholder_homeserver,
    normalize_bot_credentials,
    prefers_local_homeserver_first,
)
from src.core.types import MatrixConfig


def test_build_matrix_settings_gomatrix_defaults(monkeypatch):
    monkeypatch.delenv("COARA_MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("COARA_MATRIX_USER", raising=False)
    monkeypatch.delenv("COARA_MATRIX_PASSWORD", raising=False)
    settings = build_matrix_settings(MatrixConfig())
    assert settings["homeserver"] == "http://127.0.0.1:8008"
    assert settings["user"] == "@coara:coara.local"
    assert settings["password"] == ""  # 无默认密码：缺失保持为空，由调用方报错


def test_build_matrix_settings_keeps_working_url(monkeypatch):
    monkeypatch.setenv("COARA_MATRIX_HOMESERVER", "http://127.0.0.1:8008")
    settings = build_matrix_settings(MatrixConfig())
    assert settings["homeserver"] == "http://127.0.0.1:8008"


def test_build_matrix_settings_replaces_docs_placeholder(monkeypatch):
    monkeypatch.setenv("COARA_MATRIX_HOMESERVER", "https://your-url.trycloudflare.com")
    settings = build_matrix_settings(MatrixConfig())
    assert settings["homeserver"] == "http://127.0.0.1:8008"


def test_placeholder_and_tunnel_prefer_local():
    assert is_placeholder_homeserver("https://your-url.trycloudflare.com")
    assert prefers_local_homeserver_first("https://abc.trycloudflare.com")
    assert not prefers_local_homeserver_first("http://127.0.0.1:8008")
    assert not prefers_local_homeserver_first("https://matrix.example.com")


def test_normalize_bot_credentials_fills_empty_user():
    user, password = normalize_bot_credentials("", "", "coara.local")
    assert user == "@coara:coara.local"
    # 无默认密码：缺失密码保持为空，由调用方决定是否报错
    assert password == ""


def test_normalize_placeholder_password_keeps_coara_user():
    user, password = normalize_bot_credentials(
        "@coara:coara.local",
        "changeme",
        "coara.local",
    )
    assert user == "@coara:coara.local"
    assert password == "changeme"


def test_normalize_placeholder_password_keeps_custom_user():
    user, password = normalize_bot_credentials(
        "@alice:coara.local",
        "changeme",
        "coara.local",
    )
    assert user == "@alice:coara.local"
    assert password == "changeme"


@pytest.mark.asyncio
async def test_prepare_rejects_placeholder_password_without_homeserver_migrate(monkeypatch):
    from src.cli.matrix_connect import prepare_matrix_connection

    monkeypatch.setenv("COARA_MATRIX_HOMESERVER", "http://127.0.0.1:8008")
    monkeypatch.setenv("COARA_MATRIX_USER", "@coara:coara.local")
    monkeypatch.setenv("COARA_MATRIX_PASSWORD", "agent-password")

    async def fake_resolve(homeserver: str, port: int):
        return "http://127.0.0.1:8008", False

    persisted: list[tuple[str, str]] = []

    def fake_persist(user: str, password: str) -> None:
        persisted.append((user, password))

    monkeypatch.setattr("src.cli.matrix_connect.resolve_reachable_homeserver", fake_resolve)
    monkeypatch.setattr("src.cli.matrix_connect.persist_matrix_bot_credentials", fake_persist)

    ok, cfg, warnings = await prepare_matrix_connection(MatrixConfig())
    assert ok is False
    assert cfg is None
    assert persisted == []  # 绝不写回占位符密码
    assert any("密码" in w for w in warnings)


@pytest.mark.asyncio
async def test_resolve_reachable_homeserver_falls_back_to_8008(monkeypatch):
    from src.cli.matrix_connect import resolve_reachable_homeserver

    async def fake_check(url: str) -> bool:
        return url.rstrip("/") == "http://127.0.0.1:8008"

    async def fake_ensure(_port: int) -> bool:
        return True

    monkeypatch.setattr("src.cli.matrix_connect.check_homeserver_reachable", fake_check)
    monkeypatch.setattr("src.cli.matrix_connect.ensure_local_gomatrix", fake_ensure)

    url, migrated = await resolve_reachable_homeserver("http://127.0.0.1:9999", 9999)
    assert url == "http://127.0.0.1:8008"
    assert migrated is True


@pytest.mark.asyncio
async def test_resolve_uses_configured_when_reachable(monkeypatch):
    from src.cli.matrix_connect import resolve_reachable_homeserver

    async def fake_check(url: str) -> bool:
        return url.rstrip("/") == "http://127.0.0.1:8008"

    async def fake_ensure(_port: int) -> bool:
        return True

    monkeypatch.setattr("src.cli.matrix_connect.check_homeserver_reachable", fake_check)
    monkeypatch.setattr("src.cli.matrix_connect.ensure_local_gomatrix", fake_ensure)

    url, migrated = await resolve_reachable_homeserver("http://127.0.0.1:8008", 8008)
    assert url == "http://127.0.0.1:8008"
    assert migrated is False


@pytest.mark.asyncio
async def test_resolve_tunnel_falls_back_to_local(monkeypatch):
    from src.cli.matrix_connect import resolve_reachable_homeserver

    async def fake_check(url: str) -> bool:
        return url.rstrip("/") == "http://127.0.0.1:8008"

    async def fake_ensure(_port: int) -> bool:
        return True

    monkeypatch.setattr("src.cli.matrix_connect.check_homeserver_reachable", fake_check)
    monkeypatch.setattr("src.cli.matrix_connect.ensure_local_gomatrix", fake_ensure)

    url, migrated = await resolve_reachable_homeserver("https://random-name.trycloudflare.com", 8008)
    assert url == "http://127.0.0.1:8008"
    assert migrated is True
