"""Tests for src/ui/web_link.py（host/port 与 token 解析、URL 组装）。

这些用例原先挂在 tests/test_cli/test_attach_client.py（薄客户端）上，经其再导出
别名取 web_link 的函数；薄客户端已删，用例直接对着 web_link 本体。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.ui.dashboard_tokens import load_or_create_dashboard_token
from src.ui.web_link import load_web_token, resolve_web_host_port


class TestHostPortResolution:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_WEB_PORT", raising=False)
        assert resolve_web_host_port() == ("127.0.0.1", 8080)

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COARA_WEB_PORT", "9123")
        assert resolve_web_host_port() == ("127.0.0.1", 9123)

    def test_invalid_port_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COARA_WEB_PORT", "not-a-number")
        assert resolve_web_host_port() == ("127.0.0.1", 8080)


class TestTokenResolution:
    def test_reads_home_level_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_HOME", raising=False)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        token = load_web_token(workspace)
        token_file = workspace / ".coara" / "system" / "dashboard_token"
        assert token_file.is_file()
        assert token == token_file.read_text(encoding="utf-8").strip()

    def test_matches_server_token_for_same_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_HOME", raising=False)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        client_token = load_web_token(workspace)
        server_token = load_or_create_dashboard_token(workspace, workspace / ".coara")
        assert client_token == server_token

    def test_respects_configured_coara_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_HOME", raising=False)
        home = tmp_path / "shared-home"
        (home / "users" / "default").mkdir(parents=True)
        (home / "users" / "default" / "config.yaml").write_text(
            yaml.safe_dump({"coara_home": str(home)}), encoding="utf-8"
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("COARA_HOME", str(home))
        token = load_web_token(workspace)
        assert (home / "system" / "dashboard_token").is_file()
        assert token == (home / "system" / "dashboard_token").read_text(encoding="utf-8").strip()
