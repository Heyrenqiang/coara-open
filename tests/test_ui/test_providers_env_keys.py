"""web 保存 key → home .env + has_key envelope + 默认对齐（有 key 的声明序首位）。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.core.config import config_manager, reset_config_manager_for_tests


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config_manager_for_tests()
    yield
    reset_config_manager_for_tests()


def _load_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, providers_yaml: str) -> Path:
    from src.core.coara_home import system_dir_for_home

    home = tmp_path / "home"
    monkeypatch.setenv("COARA_HOME", str(home))
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [str(home)])
    system_dir = system_dir_for_home(home)
    system_dir.mkdir(parents=True)
    (system_dir / "providers.yaml").write_text(providers_yaml, encoding="utf-8")
    return system_dir


def _reload_config() -> None:
    asyncio.run(config_manager.load())


class TestMigrateInlineKeys:
    def test_inline_key_moves_to_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        system_dir = _load_with(
            tmp_path,
            monkeypatch,
            "providers:\n  zhipu:\n    base_url: https://x\n    api_key_env: ZHIPU_API_KEY\n",
        )
        from src.core.config import migrate_inline_api_keys_to_env

        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        migrated = migrate_inline_api_keys_to_env(
            {"zhipu": {"base_url": "https://x", "api_key_env": "ZHIPU_API_KEY", "api_key": "sk-live"}}
        )
        assert "api_key" not in migrated["zhipu"]
        assert migrated["zhipu"]["api_key_env"] == "ZHIPU_API_KEY"
        assert "ZHIPU_API_KEY=sk-live" in (system_dir / ".env").read_text(encoding="utf-8")
        assert os.environ["ZHIPU_API_KEY"] == "sk-live"  # 当前进程立即生效

    def test_custom_provider_without_env_name_gets_convention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        system_dir = _load_with(tmp_path, monkeypatch, "providers: {}\n")
        from src.core.config import migrate_inline_api_keys_to_env

        migrated = migrate_inline_api_keys_to_env({"my-shop": {"base_url": "https://x", "api_key": "sk-x"}})
        assert migrated["my-shop"]["api_key_env"] == "MY_SHOP_API_KEY"
        assert "MY_SHOP_API_KEY=sk-x" in (system_dir / ".env").read_text(encoding="utf-8")

    def test_masked_and_empty_keys_untouched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        system_dir = _load_with(tmp_path, monkeypatch, "providers: {}\n")
        from src.core.config import migrate_inline_api_keys_to_env

        migrated = migrate_inline_api_keys_to_env(
            {
                "a": {"base_url": "https://x", "api_key_env": "A_API_KEY", "api_key": "***"},
                "b": {"base_url": "https://y", "api_key_env": "B_API_KEY", "api_key": ""},
                "c": {"base_url": "https://z", "api_key_env": "C_API_KEY", "api_key": "your-key"},
            }
        )
        # 掩码/空/占位符都不写 .env；掩码与空值字段原样保留给上层语义处理
        assert migrated["a"]["api_key"] == "***"
        assert migrated["b"]["api_key"] == ""
        assert not (system_dir / ".env").exists()


class TestEnabledProviderNames:
    def test_placeholder_env_key_excluded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _load_with(
            tmp_path,
            monkeypatch,
            "providers:\n"
            "  deepseek:\n    base_url: https://d\n    api_key_env: DEEPSEEK_API_KEY\n"
            "  minimax:\n    base_url: https://m\n    api_key_env: MINIMAX_API_KEY\n",
        )
        _reload_config()
        from src.llm.model_catalog import _enabled_provider_names

        monkeypatch.setenv("DEEPSEEK_API_KEY", "your-key")  # 占位符不算有 key
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-real-minimax")
        assert _enabled_provider_names(config_manager) == ["minimax"]


class TestResolverFallback:
    def test_resolve_prefers_provider_with_usable_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无默认绑定时 resolver 回退到有 key 的 provider；两家都没 key 时按声明序（非字母序）。"""
        from src.core.config import config_manager
        from src.llm.profile_resolver import ProfileResolver, build_default_profiles
        from src.llm.profiles import Profile
        from src.llm.registry import ProviderRegistry

        def _fake(name: str, key: str, model: str) -> SimpleNamespace:
            return SimpleNamespace(
                name=name, api_key=key, default_model=model,
                base_url=f"https://{name}", get_default_max_tokens=lambda m: None,
            )

        # deepseek 声明在 minimax 前、且两家都没 key → 回退取声明序首个（deepseek），
        # 不是字母序首个（minimax）——注册表 list_available() 是 sorted。
        monkeypatch.setattr(config_manager, "list_providers", lambda: ["deepseek", "minimax"], raising=False)

        registry = ProviderRegistry()
        registry.register("deepseek", _fake("deepseek", "", "deepseek-flash"))
        registry.register("minimax", _fake("minimax", "sk-real-minimax", "MiniMax-M3"))
        import src.llm.registry as registry_module

        monkeypatch.setattr(registry_module, "provider_registry", registry)

        resolver = ProfileResolver()
        resolver.configure(
            build_default_profiles(default_provider="", default_model=""),
            default_profile=Profile.DEFAULT,
        )
        resolved = resolver.resolve(Profile.AGENT_MAIN)
        assert resolved.provider_name == "minimax"
        assert resolved.model == "MiniMax-M3"

        # 都没 key：按声明序兜底（防字母序漂移）
        registry2 = ProviderRegistry()
        registry2.register("deepseek", _fake("deepseek", "", "deepseek-flash"))
        registry2.register("minimax", _fake("minimax", "", "MiniMax-M3"))
        monkeypatch.setattr(registry_module, "provider_registry", registry2)
        resolver2 = ProfileResolver()
        resolver2.configure(
            build_default_profiles(default_provider="", default_model=""),
            default_profile=Profile.DEFAULT,
        )
        assert resolver2.resolve(Profile.AGENT_MAIN).provider_name == "deepseek"


class TestHealAlignment:
    def _patch(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_provider: str) -> list[str]:
        from src.cli import first_run_setup

        providers = [
            SimpleNamespace(name="deepseek", api_key_env="DEEPSEEK_API_KEY", api_key="", enabled=True),
            SimpleNamespace(name="minimax", api_key_env="MINIMAX_API_KEY", api_key="", enabled=True),
        ]
        monkeypatch.setattr(
            first_run_setup.config_manager, "_providers", {p.name: p for p in providers}, raising=False
        )
        monkeypatch.setattr(
            first_run_setup.config_manager,
            "_config",
            SimpleNamespace(default_provider=default_provider, coara_home=tmp_path),
            raising=False,
        )
        written: list[str] = []
        monkeypatch.setattr(
            first_run_setup,
            "write_default_provider",
            lambda name: written.append(name) or Path("/tmp/prefs"),
        )
        return written

    def test_heal_aligns_stale_keyless_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """发布版 bug 形态：默认绑在没 key 的 deepseek，填了 minimax key 后 heal 换绑。"""
        from src.cli import first_run_setup

        written = self._patch(monkeypatch, tmp_path, "deepseek")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-real-minimax")
        assert first_run_setup.heal_default_provider_if_needed(None) == "minimax"
        assert written == ["minimax"]

    def test_heal_keeps_valid_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from src.cli import first_run_setup

        written = self._patch(monkeypatch, tmp_path, "minimax")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-real-minimax")
        assert first_run_setup.heal_default_provider_if_needed(None) is None
        assert written == []


def test_save_providers_yaml_keeps_sibling_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归护栏：providers.yaml 的 default_profile/llm_profiles 等兄弟键不被保存抹掉。"""
    system_dir = _load_with(
        tmp_path,
        monkeypatch,
        "default_profile: agent.main\ndefault_provider: minimax\nproviders:\n  minimax:\n    base_url: https://m\n",
    )
    _reload_config()
    config_manager.save_providers_yaml({"providers": {"minimax": {"base_url": "https://m2"}}})
    saved = yaml.safe_load((system_dir / "providers.yaml").read_text(encoding="utf-8"))
    assert saved["default_profile"] == "agent.main"
    assert saved["default_provider"] == "minimax"
    assert saved["providers"]["minimax"]["base_url"] == "https://m2"
