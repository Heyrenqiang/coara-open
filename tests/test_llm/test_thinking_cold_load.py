"""思考档位经 ConfigManager 冷加载后仍生效。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.config import ConfigManager
from src.core.types import LLMProfileConfig
from src.llm import thinking_mode
from src.llm.model_persist import persist_thinking_setting
from src.llm.profiles import Profile
from src.llm.thinking_mode import is_enabled, load_persisted_thinking, persisted_thinking_value


@pytest.fixture(autouse=True)
def _reset_thinking():
    thinking_mode.reset_for_tests()
    yield
    thinking_mode.reset_for_tests()


def test_load_llm_profiles_preserves_thinking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """llm_preferences 里的 thinking 必须进入 typed profile，供 configure 装载基线。"""
    prefs = tmp_path / "users" / "default" / "llm_preferences.yaml"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        yaml.dump(
            {
                "default_provider": "minimax",
                "default_model": "MiniMax-M3",
                "llm_profiles": {Profile.AGENT_MAIN: {"thinking": "high"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("COARA_HOME", str(tmp_path))

    mgr = ConfigManager()
    mgr._raw_config = {
        "coara_home": str(tmp_path),
        "providers": {},
        "llm_profiles": {},
    }
    mgr._load_llm_preferences_files()
    mgr._load_llm_profiles()
    profile = mgr.get_llm_profile(Profile.AGENT_MAIN)
    assert profile.thinking == "high"

    # 模拟 llm_service.configure 装载路径（不依赖完整 Config 对象）
    load_persisted_thinking(profile.thinking)
    assert persisted_thinking_value() == "high"
    assert is_enabled() is True


def test_persist_thinking_clear_removes_raw_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COARA_HOME", str(tmp_path))
    mgr = ConfigManager()
    mgr._raw_config = {"coara_home": str(tmp_path), "llm_profiles": {Profile.AGENT_MAIN: {"thinking": "low"}}}
    mgr._llm_profiles[Profile.AGENT_MAIN] = LLMProfileConfig(thinking="low")
    # persist 会调 llm_service.configure，需要最小 _config
    from src.core.types import CoaraConfig

    mgr._config = CoaraConfig(llm_profiles=mgr._llm_profiles)
    load_persisted_thinking("low")

    persist_thinking_setting(mgr, "")
    raw_main = mgr._raw_config["llm_profiles"][Profile.AGENT_MAIN]
    assert not raw_main.get("thinking")
    assert mgr._llm_profiles[Profile.AGENT_MAIN].thinking == ""
    assert persisted_thinking_value() == ""


def test_yaml_model_thinking_preferred_over_agent_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """providers available[].thinking 优先于 agent.main.thinking。"""
    from types import SimpleNamespace

    from src.llm.thinking_mode import apply_config_thinking, resolve_config_thinking

    monkeypatch.setenv("COARA_HOME", str(tmp_path))
    mgr = ConfigManager()
    mgr._raw_config = {"coara_home": str(tmp_path), "llm_profiles": {}}
    mgr._llm_profiles[Profile.AGENT_MAIN] = LLMProfileConfig(thinking="low")
    mgr.get_provider = lambda name: SimpleNamespace(  # type: ignore[method-assign]
        models={
            "available": [
                {"id": "MiniMax-M3", "thinking": "high"},
                {"id": "other", "thinking": "off"},
            ]
        }
    )
    mgr.get_llm_profile = lambda name: mgr._llm_profiles[name]  # type: ignore[method-assign]

    assert resolve_config_thinking(mgr, "minimax", "MiniMax-M3") == "high"
    assert resolve_config_thinking(mgr, "minimax", "unknown") == "low"
    apply_config_thinking(mgr, "minimax", "MiniMax-M3")
    assert persisted_thinking_value() == "high"
    assert is_enabled() is True
