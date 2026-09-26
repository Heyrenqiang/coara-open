from __future__ import annotations

import os

import pytest

from src.core.errors import ConfigError
from src.ui.tool_credentials import credential_fields, save_tool_credentials


def test_secret_fields_never_echo_the_key(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-secret")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    rows = {row["id"]: row for row in credential_fields("web_search")}
    assert rows["EXA_API_KEY"]["set"] is True
    assert "value" not in rows["EXA_API_KEY"]
    assert rows["SERPER_API_KEY"]["set"] is False
    dumped = str(rows)
    assert "exa-secret" not in dumped


def test_save_writes_env_and_skips_blank(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("src.core.api_keys.system_env_path", lambda: env_file)
    monkeypatch.delenv("AGNES_MEDIA_API_KEY", raising=False)
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    called: list[object] = []
    monkeypatch.setattr(
        "src.coara.runtime_tools.register_media_if_configured",
        lambda root: called.append(root),
    )

    with pytest.raises(ConfigError):
        save_tool_credentials("media", {"AGNES_MEDIA_API_KEY": "  "})

    written = save_tool_credentials("media", {"AGNES_MEDIA_API_KEY": "agnes-key"}, root="root")
    assert written == ["AGNES_MEDIA_API_KEY"]
    assert os.environ["AGNES_MEDIA_API_KEY"] == "agnes-key"
    assert "AGNES_MEDIA_API_KEY=agnes-key" in env_file.read_text(encoding="utf-8")
    assert called == ["root"]
    rows = {row["id"]: row for row in credential_fields("media")}
    assert rows["AGNES_MEDIA_API_KEY"]["set"] is True
    assert "agnes-key" not in str(rows)


def test_unknown_tool_and_field_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.api_keys.system_env_path", lambda: tmp_path / ".env")
    with pytest.raises(ConfigError):
        save_tool_credentials("shell", {"AGNES_MEDIA_API_KEY": "x"})
    with pytest.raises(ConfigError):
        save_tool_credentials("web_search", {"HOME": "/tmp"})
    with pytest.raises(ConfigError):
        save_tool_credentials("media", {"AGNES_API_KEY": "x"})
