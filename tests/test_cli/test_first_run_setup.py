"""Tests for first-run key gate and default-provider heal."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.cli import first_run_setup
from src.cli.first_run_setup import (
    maybe_run_first_run_setup,
    providers_missing_keys,
)
from src.core.api_keys import write_api_key


def _make_provider(name: str, env: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, api_key_env=env, base_url=f"https://{name}.example.com")


def _patch_providers(monkeypatch: pytest.MonkeyPatch, providers: list[SimpleNamespace]) -> None:
    monkeypatch.setattr(
        first_run_setup.config_manager,
        "_providers",
        {p.name: p for p in providers},
        raising=False,
    )


def _patch_tty(monkeypatch: pytest.MonkeyPatch, is_tty: bool) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: is_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)


def test_is_tty_tolerates_missing_stream() -> None:
    """无标准流（内核自重启拉起的子进程）按非终端处理，不能抛错把启动带走。"""
    from src.cli.first_run_setup import _is_tty

    class _Tty:
        def isatty(self) -> bool:
            return True

    class _Boom:
        def isatty(self) -> bool:
            raise OSError("bad fd")

    assert _is_tty(None) is False
    assert _is_tty(object()) is False
    assert _is_tty(_Tty()) is True
    assert _is_tty(_Boom()) is False


def test_providers_missing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = [_make_provider("a", "KEY_A"), _make_provider("b", "KEY_B")]
    _patch_providers(monkeypatch, providers)
    monkeypatch.delenv("KEY_A", raising=False)
    monkeypatch.setenv("KEY_B", "  secret  ")

    missing = providers_missing_keys()
    assert [p.name for p in missing] == ["a"]


def test_configured_providers_preferred_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wizard / heal list follows YAML declaration order (user-arranged), enabled only."""
    from src.cli.first_run_setup import _configured_providers

    providers = [
        _make_provider("agnes", "KEY_A"),
        _make_provider("minimax", "KEY_M"),
        _make_provider("deepseek", "KEY_D"),
        _make_provider("zhipu", "KEY_Z"),
        _make_provider("kimi", "KEY_K"),
    ]
    _patch_providers(monkeypatch, providers)
    assert [p.name for p in _configured_providers()] == [
        "agnes",
        "minimax",
        "deepseek",
        "zhipu",
        "kimi",
    ]


def test_heal_prefers_first_declared_over_later(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.cli.first_run_setup import heal_default_provider_if_needed

    providers = [_make_provider("agnes", "KEY_A"), _make_provider("deepseek", "KEY_D")]
    _patch_providers(monkeypatch, providers)
    monkeypatch.setenv("KEY_A", "real-agnes-key")
    monkeypatch.setenv("KEY_D", "real-deepseek-key")
    monkeypatch.setattr(
        first_run_setup.config_manager,
        "_config",
        SimpleNamespace(default_provider="missing"),
        raising=False,
    )
    written: list[str] = []

    def _fake_write(name: str) -> Path:
        written.append(name)
        return Path("/tmp/prefs")

    monkeypatch.setattr(first_run_setup, "write_default_provider", _fake_write)
    # 纯声明序：不再对产品 DEFAULT_PROVIDER 加权——用户填了谁家的 key 默认就落谁家，
    # agnes 声明在前且有 key 即取 agnes（写死偏好会把只填了 minimax 的新装机对齐到
    # 没 key 的 deepseek，2026-09-23 发布版默认模型错配 bug 的修法）。
    assert heal_default_provider_if_needed(None) == "agnes"
    assert written == ["agnes"]


def test_placeholder_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.api_keys import is_usable_api_key

    assert not is_usable_api_key("")
    assert not is_usable_api_key("your-key")
    assert not is_usable_api_key("xxx")
    assert not is_usable_api_key("changeme")
    assert is_usable_api_key("sk-live-real-key")

    providers = [_make_provider("a", "KEY_A")]
    _patch_providers(monkeypatch, providers)
    monkeypatch.setenv("KEY_A", "your-key")
    assert providers_missing_keys()[0].name == "a"


def test_heal_default_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.cli.first_run_setup import heal_default_provider_if_needed

    providers = [_make_provider("agnes", "KEY_A"), _make_provider("minimax", "KEY_M")]
    _patch_providers(monkeypatch, providers)
    monkeypatch.delenv("KEY_A", raising=False)
    monkeypatch.setenv("KEY_M", "real-minimax-key")
    monkeypatch.setattr(
        first_run_setup.config_manager,
        "_config",
        SimpleNamespace(default_provider="agnes", coara_home=tmp_path),
        raising=False,
    )

    def _fake_write(name: str) -> Path:
        first_run_setup.config_manager._config.default_provider = name  # noqa: SLF001
        return tmp_path / "prefs.yaml"

    monkeypatch.setattr(first_run_setup, "write_default_provider", _fake_write)
    assert heal_default_provider_if_needed(None) == "minimax"


def _make_full_provider(name: str, env: str, default_model: str, available: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        api_key_env=env,
        base_url=f"https://{name}.example.com",
        models={"default": default_model, "available": [{"id": m} for m in available]},
        default_model=default_model,
        enabled=True,
    )


def _patch_prefs_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_provider: str, default_model: str):
    monkeypatch.setattr(
        first_run_setup.config_manager,
        "_config",
        SimpleNamespace(default_provider=default_provider, default_model=default_model, coara_home=tmp_path),
        raising=False,
    )
    monkeypatch.setattr(first_run_setup.config_manager, "merge_config", lambda d: None, raising=False)
    monkeypatch.setattr(first_run_setup.config_manager, "_llm_profiles", {}, raising=False)
    monkeypatch.setattr(
        "src.llm.model_persist._resolve_persist_call_settings",
        lambda *a, **k: (None, None),
    )


def test_write_default_provider_reconciles_split_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fallback heal must not leave a cross-vendor pair like agnes + MiniMax-M2.7:
    default_model realigns, and agent.main follows when its provider is keyless."""
    _patch_providers(
        monkeypatch,
        [
            _make_full_provider("agnes", "KEY_A", "agnes-2.5-flash", ["agnes-2.5-flash", "agnes-2.0-flash"]),
            _make_full_provider("minimax", "KEY_M", "MiniMax-M2.7-highspeed", ["MiniMax-M2.7-highspeed", "MiniMax-M3"]),
        ],
    )
    monkeypatch.setenv("KEY_A", "real-agnes-key")
    monkeypatch.delenv("KEY_M", raising=False)  # minimax keyless → agent.main 一并治
    prefs = tmp_path / "users" / "default" / "llm_preferences.yaml"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        "default_provider: minimax\n"
        "default_model: MiniMax-M2.7-highspeed\n"
        "llm_profiles:\n  agent.main:\n    provider: minimax\n    model: MiniMax-M2.7-highspeed\n",
        encoding="utf-8",
    )
    _patch_prefs_home(monkeypatch, tmp_path, "minimax", "MiniMax-M2.7-highspeed")

    first_run_setup.write_default_provider("agnes")

    data = yaml.safe_load(prefs.read_text(encoding="utf-8"))
    assert data["default_provider"] == "agnes"
    assert data["default_model"] == "agnes-2.5-flash"  # 不再交叉
    assert data["llm_profiles"]["agent.main"]["provider"] == "agnes"
    assert data["llm_profiles"]["agent.main"]["model"] == "agnes-2.5-flash"
    cfg = first_run_setup.config_manager._config  # noqa: SLF001
    assert cfg.default_provider == "agnes"
    assert cfg.default_model == "agnes-2.5-flash"


def test_write_default_provider_keeps_keyed_agent_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """agent.main pointing at a provider WITH a usable key is an explicit choice — keep it."""
    _patch_providers(
        monkeypatch,
        [
            _make_full_provider("agnes", "KEY_A", "agnes-2.5-flash", ["agnes-2.5-flash"]),
            _make_full_provider("minimax", "KEY_M", "MiniMax-M2.7-highspeed", ["MiniMax-M2.7-highspeed"]),
        ],
    )
    monkeypatch.setenv("KEY_A", "real-agnes-key")
    monkeypatch.setenv("KEY_M", "real-minimax-key")
    prefs = tmp_path / "users" / "default" / "llm_preferences.yaml"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        "default_provider: deepseek\n"
        "default_model: deepseek-flash\n"
        "llm_profiles:\n  agent.main:\n    provider: minimax\n    model: MiniMax-M2.7-highspeed\n",
        encoding="utf-8",
    )
    _patch_prefs_home(monkeypatch, tmp_path, "deepseek", "deepseek-flash")

    first_run_setup.write_default_provider("agnes")

    data = yaml.safe_load(prefs.read_text(encoding="utf-8"))
    assert data["default_provider"] == "agnes"
    assert data["default_model"] == "agnes-2.5-flash"
    assert data["llm_profiles"]["agent.main"]["provider"] == "minimax"  # 有 key 的显式选择不动
    assert data["llm_profiles"]["agent.main"]["model"] == "MiniMax-M2.7-highspeed"


def test_write_api_key_creates_file(tmp_path: Path) -> None:
    env_path = tmp_path / "system" / ".env"
    write_api_key(env_path, "MY_KEY", "v1")
    assert env_path.read_text(encoding="utf-8") == "MY_KEY=v1\n"


def test_write_api_key_replaces_commented_placeholder(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# header\n# MY_KEY=\nOTHER=1\n", encoding="utf-8")
    write_api_key(env_path, "MY_KEY", "v2")
    assert env_path.read_text(encoding="utf-8") == "# header\nMY_KEY=v2\nOTHER=1\n"


def test_write_api_key_replaces_existing_and_appends(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("MY_KEY=old\n", encoding="utf-8")
    write_api_key(env_path, "MY_KEY", "new")
    write_api_key(env_path, "SECOND_KEY", "s")
    assert env_path.read_text(encoding="utf-8") == "MY_KEY=new\nSECOND_KEY=s\n"


@pytest.mark.asyncio
async def test_wizard_skips_when_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_providers(monkeypatch, [_make_provider("a", "KEY_A")])
    monkeypatch.delenv("KEY_A", raising=False)
    _patch_tty(monkeypatch, is_tty=False)

    assert await maybe_run_first_run_setup(SimpleNamespace(print=lambda *a, **k: None)) is False


@pytest.mark.asyncio
async def test_wizard_skips_when_any_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_providers(monkeypatch, [_make_provider("a", "KEY_A"), _make_provider("b", "KEY_B")])
    monkeypatch.delenv("KEY_A", raising=False)
    monkeypatch.setenv("KEY_B", "secret")
    _patch_tty(monkeypatch, is_tty=True)

    assert await maybe_run_first_run_setup(SimpleNamespace(print=lambda *a, **k: None)) is False


@pytest.mark.asyncio
async def test_first_run_prompts_web_config_when_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 key 时只提示去配置页「模型」，不写 .env、不跑终端向导。"""
    _patch_providers(monkeypatch, [_make_provider("a", "KEY_A")])
    monkeypatch.delenv("KEY_A", raising=False)
    _patch_tty(monkeypatch, is_tty=True)
    printed: list[str] = []

    def _print(*args: object, **_kwargs: object) -> None:
        printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(
        first_run_setup,
        "build_web_url",
        lambda *_a, **_k: None,
        raising=False,
    )
    # build_web_url is imported inside the function; patch the module it imports from
    import src.ui.web_link as web_link

    monkeypatch.setattr(web_link, "build_web_url", lambda *_a, **_k: "http://127.0.0.1:8080/config?focus=models")

    assert await maybe_run_first_run_setup(SimpleNamespace(print=_print)) is False
    blob = "\n".join(printed)
    assert "配置模型" in blob or "模型" in blob
    assert "focus=models" in blob


@pytest.mark.asyncio
async def test_first_run_no_longer_writes_env_via_wizard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_providers(monkeypatch, [_make_provider("a", "KEY_A")])
    monkeypatch.delenv("KEY_A", raising=False)
    _patch_tty(monkeypatch, is_tty=True)
    env_path = tmp_path / "system" / ".env"
    monkeypatch.setattr("src.core.api_keys.system_env_path", lambda: env_path)

    assert await maybe_run_first_run_setup(SimpleNamespace(print=lambda *a, **k: None)) is False
    assert not env_path.exists()
