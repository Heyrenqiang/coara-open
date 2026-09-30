"""home_upgrade：出厂目录 rebase / 退役清理 / 自定义保留。"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.home_upgrade import (
    RETIRED_STOCK_PROVIDERS,
    upgrade_home_system,
    upgrade_providers_yaml,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_rebase_retires_agnes_keeps_custom_and_env_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    system = home / "system"
    templates = tmp_path / "templates"

    _write(
        templates / "providers.yaml",
        """
default_provider: ""
providers:
  deepseek:
    driver: responses
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-flash
    models:
      default: deepseek-flash
      available:
        - id: deepseek-flash
          name: DeepSeek Flash
  minimax:
    driver: responses
    base_url: https://api.minimaxi.com/v1
    api_key_env: MINIMAX_API_KEY
    default_model: MiniMax-M3
    models:
      default: MiniMax-M3
      available:
        - id: MiniMax-M3
          name: MiniMax M3
llm_profiles:
  agent.deepseek:
    provider: deepseek
    model: deepseek-flash
  agent.minimax:
    provider: minimax
    model: MiniMax-M3
""",
    )
    _write(templates / "env.example", "# fresh example\nDEEPSEEK_API_KEY=\n")
    _write(templates / "config.yaml", "coara_home: null\n")

    _write(
        system / "providers.yaml",
        """
default_provider: agnes
providers:
  deepseek:
    driver: openai
    base_url: https://old.example
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-chat
    models:
      available:
        - id: deepseek-chat
  agnes:
    driver: openai
    base_url: https://api.agnes-ai.cn/v1
    api_key_env: AGNES_API_KEY
    default_model: agnes-2.5-flash
    models:
      available:
        - id: agnes-2.5-flash
  mycorp:
    driver: openai
    base_url: https://llm.mycorp.local/v1
    api_key_env: MYCORP_API_KEY
    default_model: corp-1
    models:
      available:
        - id: corp-1
llm_profiles:
  agent.agnes:
    provider: agnes
    model: agnes-2.5-flash
  agent.mycorp:
    provider: mycorp
    model: corp-1
""",
    )
    env_path = system / ".env"
    _write(env_path, "DEEPSEEK_API_KEY=sk-keep-me\nAGNES_API_KEY=sk-agnes\nMYCORP_API_KEY=sk-corp\n")
    _write(system / "env.example", "# stale\n")

    report = upgrade_home_system(home, templates_dir=templates, product_version="9.9.9")
    assert report.providers_written
    assert "agnes" in report.retired_providers
    assert "mycorp" in report.preserved_custom
    assert "deepseek" in report.rebased_providers
    assert "minimax" in report.rebased_providers
    assert report.default_provider_cleared == "agnes"

    data = yaml.safe_load((system / "providers.yaml").read_text(encoding="utf-8"))
    assert "agnes" not in data["providers"]
    assert "mycorp" in data["providers"]
    assert data["providers"]["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert data["providers"]["deepseek"]["default_model"] == "deepseek-flash"
    assert data["default_provider"] == ""
    assert "agent.agnes" not in (data.get("llm_profiles") or {})
    assert "agent.mycorp" in data["llm_profiles"]

    # .env 原样
    assert env_path.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=sk-keep-me\nAGNES_API_KEY=sk-agnes\nMYCORP_API_KEY=sk-corp\n"
    assert "fresh example" in (system / "env.example").read_text(encoding="utf-8")
    assert (system / "home_schema.yaml").is_file()
    assert "agnes" in RETIRED_STOCK_PROVIDERS


def test_upgrade_preserves_user_extra_models_on_stock(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    user = tmp_path / "providers.yaml"
    _write(
        templates / "providers.yaml",
        """
providers:
  deepseek:
    driver: responses
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-flash
    models:
      available:
        - id: deepseek-flash
""",
    )
    _write(
        user,
        """
providers:
  deepseek:
    driver: responses
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-flash
    enabled: false
    models:
      available:
        - id: deepseek-flash
        - id: deepseek-custom-lab
          name: Lab
""",
    )
    written, report = upgrade_providers_yaml(user, templates / "providers.yaml")
    assert written
    data = yaml.safe_load(user.read_text(encoding="utf-8"))
    ids = [x["id"] if isinstance(x, dict) else x for x in data["providers"]["deepseek"]["models"]["available"]]
    assert ids == ["deepseek-flash", "deepseek-custom-lab"]
    assert data["providers"]["deepseek"]["enabled"] is False
    assert "deepseek" in report.rebased_providers


def test_scrub_preferences_and_workspace_bindings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    templates = tmp_path / "templates"
    _write(
        templates / "providers.yaml",
        """
providers:
  deepseek:
    driver: responses
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-flash
    models:
      available:
        - id: deepseek-flash
""",
    )
    _write(templates / "env.example", "DEEPSEEK_API_KEY=\n")
    prefs = home / "users" / "default" / "llm_preferences.yaml"
    _write(
        prefs,
        """
default_provider: agnes
default_model: agnes-2.5-flash
llm_profiles:
  agent.main:
    provider: agnes
    model: agnes-2.5-flash
""",
    )
    registry = home / "registry" / "workspaces.yaml"
    _write(
        registry,
        """
workspaces:
  abc:
    id: abc
    name: demo
    path: D:/ws/demo
    provider: agnes
    model: agnes-2.5-flash
""",
    )
    # 旧 providers 含 agnes，升级后应被摘掉并清绑定
    _write(
        home / "system" / "providers.yaml",
        """
providers:
  deepseek:
    driver: responses
    base_url: https://old
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-flash
    models:
      available:
        - id: deepseek-flash
  agnes:
    driver: openai
    base_url: https://api.agnes-ai.cn/v1
    api_key_env: AGNES_API_KEY
    default_model: agnes-2.5-flash
    models:
      available:
        - id: agnes-2.5-flash
""",
    )
    report = upgrade_home_system(home, templates_dir=templates, product_version="2.0.0")
    assert report.preferences_scrubbed
    assert "demo" in report.workspace_bindings_cleared
    pref_data = yaml.safe_load(prefs.read_text(encoding="utf-8"))
    assert pref_data.get("default_provider") in ("", None)
    assert "provider" not in (pref_data.get("llm_profiles") or {}).get("agent.main", {})
    reg = yaml.safe_load(registry.read_text(encoding="utf-8"))
    assert reg["workspaces"]["abc"].get("provider") in (None, "")


def test_upgrade_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    templates = tmp_path / "templates"
    _write(
        templates / "providers.yaml",
        """
providers:
  deepseek:
    driver: responses
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    default_model: deepseek-flash
    models:
      available:
        - id: deepseek-flash
""",
    )
    _write(templates / "env.example", "DEEPSEEK_API_KEY=\n")
    first = upgrade_home_system(home, templates_dir=templates, product_version="1.0.0")
    assert first.providers_written or (home / "system" / "providers.yaml").is_file()
    second = upgrade_home_system(home, templates_dir=templates, product_version="1.0.0")
    assert second.providers_written is False
