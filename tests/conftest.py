"""Shared pytest fixtures."""

from __future__ import annotations

import gc
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

# Detailed tests — excluded from default run (see pyproject.toml addopts). Run: pytest -m extended
_EXTENDED_TEST_PATHS = frozenset(
    path.replace("\\", "/")
    for path in (
        "test_agent/test_runtime.py",
        "test_agent/test_tool_policy.py",
        "test_agent/test_output_truncation.py",
        "test_agent/test_executor_truncation.py",
        "test_agent/test_shell_failure_streak.py",
        "test_background/test_tasks.py",
        "test_cli/test_main.py",
        "test_coara/test_delegate.py",
        "test_coara/test_tool_output.py",
        "test_context/test_window.py",
        "test_core/test_infra.py",
        "test_llm/test_message_content.py",
        "test_prompt/test_yaml_loader.py",
        "test_runtime/test_tool_output_store.py",
        "test_tools/test_sandbox.py",
        "test_tools/test_file_safety.py",
        "test_tools/test_file_io_tools.py",
        "test_tools/test_ws_tool.py",
        "test_tools/test_shell_tool.py",
        "test_tools/test_tool_manager.py",
        "test_tools/test_web_fetch.py",
        "test_tools/test_web_search.py",
        "test_tools/test_word_builder.py",
        "test_ui/test_web_server_port.py",
    )
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        rel = Path(str(item.fspath)).as_posix()
        if "tests/" in rel:
            rel = rel.split("tests/", 1)[1]
        if rel in _EXTENDED_TEST_PATHS:
            item.add_marker(pytest.mark.extended)


_MINIMAL_PROVIDERS_YAML = """\
default_provider: p1
providers:
  p1:
    base_url: http://127.0.0.1:9
    driver: anthropic
    api_key_env: COARA_TEST_UNUSED_KEY
    models:
      default: m1
"""


def _seed_isolated_coara_home(home: Path) -> None:
    """Lay down a minimal canonical config tree under the isolated home.

    Creates config in both new ``system/`` and legacy ``config/`` locations
    so that path resolution works regardless of which is checked first.
    """
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    home_posix = home.as_posix()
    config_yaml = f"coara_home: {home_posix}\nlog_level: WARNING\nvault_enabled: false\n"
    (config_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (config_dir / "providers.yaml").write_text(_MINIMAL_PROVIDERS_YAML, encoding="utf-8")
    # Also seed new layout (system/ directory)
    sys_dir = home / "system"
    sys_dir.mkdir(parents=True, exist_ok=True)
    (sys_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (sys_dir / "providers.yaml").write_text(_MINIMAL_PROVIDERS_YAML, encoding="utf-8")


def _patch_coara_home_resolution(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Force all bootstrap/config resolution to the isolated home (ignore registry + D:/coara)."""
    home_text = str(home)

    monkeypatch.setenv("COARA_HOME", home_text)
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [home_text])
    monkeypatch.setattr("src.core.coara_home._read_windows_registry_env", lambda _name: [])


@pytest.fixture(autouse=True)
def isolated_coara_home(
    request: pytest.FixRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path | None]:
    """Keep pytest runtime data out of the developer's real ``COARA_HOME``.

    Skipped for ``@pytest.mark.real_env`` tests that intentionally use live config.
    """
    if request.node.get_closest_marker("real_env"):
        yield None
        return

    from src.core.config import reset_config_manager_for_tests

    home = tmp_path / "coara-home"
    _seed_isolated_coara_home(home)
    _patch_coara_home_resolution(monkeypatch, home)
    reset_config_manager_for_tests()

    yield home

    reset_config_manager_for_tests()


@pytest.fixture(autouse=True)
def cleanup_global_state():
    """Reset global singletons between tests to prevent cross-test pollution."""
    yield
    # 1. Provider registry — clear all registered providers/factories
    try:
        from src.llm.registry import provider_registry

        provider_registry.clear()
    except Exception:
        pass

    # 2. LLM service — reset profile resolver (HTTP clients cleared with registry)
    try:
        from src.llm.service import llm_service

        llm_service.reset_for_tests()
    except Exception:
        pass

    # 3. Skill managers — wipe the dashboard-scoped display pool between tests
    #    (per-CoaraBase pools die with their instance and need no cleanup)
    try:
        from src.ui.control_plane import _settings_skill_manager

        _settings_skill_manager._skills.clear()
        _settings_skill_manager._skill_dirs.clear()
        _settings_skill_manager._discover_cache_key = None
        _settings_skill_manager._discover_cache_mtimes = None
    except Exception:
        pass

    # 4. Config manager cache (after test body; setup also clears in isolated_coara_home)
    try:
        from src.core.config import reset_config_manager_for_tests

        reset_config_manager_for_tests()
    except Exception:
        pass

    try:
        from src.todos.registry import reset_todo_store_registry

        reset_todo_store_registry()
    except Exception:
        pass

    # 6. Force GC to clean up dangling transports/tasks before next test
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect(0)
