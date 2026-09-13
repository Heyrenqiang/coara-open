"""Config path resolution — new layout under <coara_home>/system/ + <coara_home>/users/default/."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from src.core.coara_home import (
    home_llm_preferences_path,
    resolve_config_home,
    system_dir_for_home,
    user_dir_for_home,
)
from src.core.config import (
    ConfigError,
    ConfigManager,
    _find_writable_config_yaml,
    _find_writable_providers_yaml,
    _iter_config_yaml_paths,
    _iter_env_file_paths,
    _iter_llm_preference_paths,
    mask_secrets,
)


@pytest.fixture
def coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coara"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [str(home)])
    return home


def test_config_loads_only_from_home_system_dir(coara_home: Path) -> None:
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    providers_yaml = (
        "default_provider: p1\n"
        "providers:\n"
        "  p1:\n"
        "    base_url: http://test\n"
        "    api_key_env: TEST_KEY\n"
        "    models:\n"
        "      default: m1\n"
    )
    (system_dir / "providers.yaml").write_text(providers_yaml, encoding="utf-8")
    (system_dir / "config.yaml").write_text("log_level: INFO\n", encoding="utf-8")

    # User-level override
    user_dir = user_dir_for_home(coara_home)
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "config.yaml").write_text("log_level: DEBUG\n", encoding="utf-8")

    manager = ConfigManager()
    asyncio.run(manager.load())

    assert manager.get_raw_config()["log_level"] == "DEBUG"
    assert manager.get_raw_config()["default_provider"] == "p1"


_PROVIDERS_YAML = (
    "default_provider: p1\n"
    "providers:\n"
    "  p1:\n"
    "    base_url: http://test\n"
    "    api_key_env: TEST_KEY\n"
    "    models:\n"
    "      default: m1\n"
)


def _write_system_config(coara_home: Path, config_yaml: str) -> None:
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "providers.yaml").write_text(_PROVIDERS_YAML, encoding="utf-8")
    (system_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")


def test_background_task_timeout_seconds_flows_into_typed_config(coara_home: Path) -> None:
    _write_system_config(coara_home, "background_task_timeout_seconds: 300\n")

    manager = ConfigManager()
    config = asyncio.run(manager.load())

    assert config.background_task_timeout_seconds == 300.0


def test_background_task_timeout_seconds_defaults_to_unset(coara_home: Path) -> None:
    _write_system_config(coara_home, "log_level: INFO\n")

    manager = ConfigManager()
    config = asyncio.run(manager.load())

    # 未设置时为 None：由 bash_runner 的代码默认（7200，0 关闭）接管
    assert config.background_task_timeout_seconds is None


def test_repo_root_config_is_ignored(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    (system_dir / "config.yaml").write_text("log_level: HOME\n", encoding="utf-8")
    (system_dir / "providers.yaml").write_text("providers: {}\n", encoding="utf-8")

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "config.yaml").write_text("log_level: WORKSPACE\n", encoding="utf-8")
    monkeypatch.chdir(workspace_dir)

    paths = _iter_config_yaml_paths()
    assert all("system" in str(p) or "users" in str(p) for p in paths)
    assert workspace_dir / "config.yaml" not in paths

    manager = ConfigManager()
    asyncio.run(manager.load())
    assert manager.get_raw_config()["log_level"] == "HOME"


def test_writable_paths_under_workspace(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    raw = {"coara_home": str(coara_home)}
    assert _find_writable_config_yaml(raw) == workspace / ".coara" / "config.yaml"
    assert _find_writable_providers_yaml(raw) == workspace / ".coara" / "providers.yaml"


def test_writable_config_prefers_user_over_system(coara_home: Path, tmp_path: Path) -> None:
    """审计 P0 #4：users/default/config.yaml 存在时写入它，而非 system/config.yaml。

    读序 users/default > system：若写入只认 system，WebUI 保存的设置会被
    users/default 层遮蔽，重启即回退。
    """
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    (system_dir / "providers.yaml").write_text("providers: {}\n", encoding="utf-8")
    (system_dir / "config.yaml").write_text("log_level: INFO\n", encoding="utf-8")

    user_config = user_dir_for_home(coara_home) / "config.yaml"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text("log_level: DEBUG\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.chdir(workspace)
    try:
        raw = {"coara_home": str(coara_home)}
        assert _find_writable_config_yaml(raw) == user_config

        # 写入落在读序最高层，保存的设置重启后仍生效
        manager = ConfigManager()
        asyncio.run(manager.load())
        manager.save_config_yaml({"cli": {"theme": "light"}})
        saved = yaml.safe_load(user_config.read_text(encoding="utf-8"))
        assert saved["cli"]["theme"] == "light"
        assert saved["log_level"] == "DEBUG"
        # system 层未被触碰
        system_saved = yaml.safe_load((system_dir / "config.yaml").read_text(encoding="utf-8"))
        assert system_saved == {"log_level": "INFO"}

        manager2 = ConfigManager()
        asyncio.run(manager2.load())
        assert manager2.get_raw_config()["cli"]["theme"] == "light"
    finally:
        monkeypatch.undo()


def test_writable_providers_prefers_system_when_present(coara_home: Path, tmp_path: Path) -> None:
    """Regression: save_providers_yaml must land on the read path (system/ first),
    otherwise Web/CLI provider edits silently disappear on next restart.
    """
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    sys_providers = system_dir / "providers.yaml"
    sys_providers.write_text("providers: {}\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.chdir(workspace)
    try:
        raw = {"coara_home": str(coara_home)}
        assert _find_writable_providers_yaml(raw) == sys_providers

        manager = ConfigManager()
        asyncio.run(manager.load())
        manager.save_providers_yaml({"providers": {"new_one": {"base_url": "http://x"}}})

        # Wrote on the read path — next reload must see it.
        manager2 = ConfigManager()
        asyncio.run(manager2.load())
        assert "new_one" in manager2.get_raw_config().get("providers", {})
    finally:
        monkeypatch.undo()


def test_save_providers_yaml_preserves_sibling_keys(coara_home: Path, tmp_path: Path) -> None:
    """Web save must not wipe default_*/llm_profiles/security from providers.yaml."""
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    sys_providers = system_dir / "providers.yaml"
    sys_providers.write_text(
        yaml.dump(
            {
                "default_profile": "agent.main",
                "default_provider": "minimax",
                "default_model": "MiniMax-M2",
                "providers": {"old": {"base_url": "http://old", "api_key_env": "K"}},
                "llm_profiles": {"agent.main": {"provider": "minimax", "model": "MiniMax-M2"}},
                "security": {"call_policy": {"auto_allow": ["read"]}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (system_dir / "config.yaml").write_text("log_level: INFO\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.chdir(workspace)
    try:
        manager = ConfigManager()
        asyncio.run(manager.load())
        manager.save_providers_yaml({"providers": {"new_one": {"base_url": "http://x", "api_key_env": "K"}}})

        saved = yaml.safe_load(sys_providers.read_text(encoding="utf-8"))
        assert saved["default_profile"] == "agent.main"
        assert saved["default_provider"] == "minimax"
        assert saved["default_model"] == "MiniMax-M2"
        assert saved["llm_profiles"]["agent.main"]["provider"] == "minimax"
        assert saved["security"]["call_policy"]["auto_allow"] == ["read"]
        assert "new_one" in saved["providers"]
        assert "old" not in saved["providers"]
    finally:
        monkeypatch.undo()


def test_llm_preferences_single_path(coara_home: Path) -> None:
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    (system_dir / "providers.yaml").write_text("providers: {}\n", encoding="utf-8")

    user_dir = user_dir_for_home(coara_home)
    user_dir.mkdir(parents=True, exist_ok=True)
    prefs = home_llm_preferences_path(coara_home)
    prefs.write_text(yaml.dump({"default_model": "m1"}), encoding="utf-8")

    assert _iter_llm_preference_paths() == [prefs]

    manager = ConfigManager()
    asyncio.run(manager.load())
    assert manager.get_raw_config()["default_model"] == "m1"


def test_env_only_from_home_system(coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".env").write_text("TEST_LAYER=WORKSPACE\n", encoding="utf-8")

    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    (system_dir / "providers.yaml").write_text("providers: {}\n", encoding="utf-8")
    (system_dir / ".env").write_text("TEST_LAYER=HOME\n", encoding="utf-8")

    env_paths = _iter_env_file_paths()
    assert system_dir / ".env" in env_paths
    # Workspace .env should never be included
    assert workspace / ".env" not in env_paths

    manager = ConfigManager()
    asyncio.run(manager.load())
    assert os.getenv("TEST_LAYER") == "HOME"


def test_missing_config_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COARA_HOME", raising=False)
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [])
    monkeypatch.chdir(tmp_path)

    manager = ConfigManager()
    with pytest.raises(ConfigError, match="providers.yaml"):
        asyncio.run(manager.load())


# --- mask_secrets：词边界匹配，不误伤普通键 ---


def test_mask_secrets_masks_exact_and_suffixed_keys() -> None:
    payload = {
        "password": "p",
        "api_key": "k",
        "matrix": {"password": "p2"},
        "api_key_env": {"MY_KEY": "v"},
        "access_token": "t",
        "authorization_header": {"value": "not-a-key"},
        "user_token": "t2",
    }
    masked = mask_secrets(payload)
    assert masked["password"] == "***"
    assert masked["api_key"] == "***"
    assert masked["matrix"]["password"] == "***"
    # MY_KEY 是 api_key_env 的值结构内部键：MY_KEY 以 _KEY 结尾，同样应被掩
    assert masked["api_key_env"]["MY_KEY"] == "***"
    assert masked["access_token"] == "***"
    assert masked["user_token"] == "***"


def test_mask_secrets_does_not_mask_substring_keys() -> None:
    """author 含 auth、max_tokens 含 token：子串误掩必须消失"""
    payload = {
        "author": "张三",
        "max_tokens": "4096",
        "tokenized_count": 5,
        "authored_by": "李四",
        "keywords": "a,b",
    }
    masked = mask_secrets(payload)
    assert masked["author"] == "张三"
    assert masked["max_tokens"] == "4096"
    assert masked["tokenized_count"] == 5
    assert masked["authored_by"] == "李四"
    assert masked["keywords"] == "a,b"


def test_mask_secrets_preserves_non_string_values_and_lists() -> None:
    """非字符串值不打掩码（数字 token 计数等），列表递归处理"""
    payload = {
        "token": 12345,
        "models": [{"api_key": "k"}, {"name": "m"}],
        "base_url": "http://x",
    }
    masked = mask_secrets(payload)
    assert masked["token"] == 12345
    assert masked["models"][0]["api_key"] == "***"
    assert masked["models"][1]["name"] == "m"
    assert masked["base_url"] == "http://x"


def test_resolve_config_home_prefers_coara_home_in_yaml(coara_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = coara_home.parent / "other"
    other.mkdir()
    monkeypatch.delenv("COARA_HOME", raising=False)
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [])
    resolved = resolve_config_home({"coara_home": str(other)})
    assert resolved == other.resolve()
