"""思考档位配置基线：装载、优先级与持久化。"""

from __future__ import annotations

import pytest

from src.llm import thinking_mode
from src.llm.thinking_mode import (
    current_level,
    is_enabled,
    load_persisted_thinking,
    persisted_thinking_value,
)


@pytest.fixture(autouse=True)
def _reset():
    thinking_mode.reset_for_tests()
    yield
    thinking_mode.reset_for_tests()


def test_default_when_unconfigured() -> None:
    assert persisted_thinking_value() == ""
    assert is_enabled() is True
    assert current_level() == "medium"


def test_load_off_disables_thinking() -> None:
    load_persisted_thinking("off")
    assert persisted_thinking_value() == "off"
    assert is_enabled() is False
    # 档位不受 off 影响
    assert current_level() == "medium"


def test_load_level_implies_enabled() -> None:
    load_persisted_thinking("high")
    assert persisted_thinking_value() == "high"
    assert is_enabled() is True
    assert current_level() == "high"


def test_invalid_value_ignored() -> None:
    load_persisted_thinking("turbo")
    assert persisted_thinking_value() == ""
    assert is_enabled() is True


def test_session_override_beats_config() -> None:
    load_persisted_thinking("low")
    thinking_mode.set_cli_thinking_enabled(True)
    thinking_mode.set_cli_thinking_level("high")
    assert is_enabled() is True
    assert current_level() == "high"
    # 会话关闭覆盖后落回配置基线
    thinking_mode.set_cli_thinking_enabled(None)
    thinking_mode.set_cli_thinking_level(None)
    assert is_enabled() is True
    assert current_level() == "low"


def test_config_beats_vendor_default_but_not_session() -> None:
    load_persisted_thinking("off")
    assert is_enabled(kind="agnes_thinking") is False  # agnes 默认关，配置关仍关
    load_persisted_thinking("on")
    assert is_enabled(kind="agnes_thinking") is True  # 配置开可覆盖 agnes 默认关


def test_describe_source_reflects_config_baseline() -> None:
    from src.llm.thinking_mode import describe_thinking_status

    enabled, source, _note = describe_thinking_status(
        "minimax-m3", base_url="https://api.minimaxi.com/v1", driver="responses"
    )
    assert enabled is True
    assert source == "默认开启"

    load_persisted_thinking("off")
    enabled, source, _note = describe_thinking_status(
        "minimax-m3", base_url="https://api.minimaxi.com/v1", driver="responses"
    )
    assert enabled is False
    assert source == "配置关闭"

    load_persisted_thinking("high")
    enabled, source, _note = describe_thinking_status(
        "minimax-m3", base_url="https://api.minimaxi.com/v1", driver="responses"
    )
    assert enabled is True
    assert source == "配置·高"

    thinking_mode.set_cli_thinking_enabled(False)
    enabled, source, _note = describe_thinking_status(
        "minimax-m3", base_url="https://api.minimaxi.com/v1", driver="responses"
    )
    assert enabled is False
    assert source == "本会话"


def test_on_without_level_leaves_explicit_level_none() -> None:
    from src.llm.thinking_mode import explicit_level

    load_persisted_thinking("on")
    assert is_enabled() is True
    assert explicit_level() is None
    assert current_level() == "medium"  # UI 回落；厂商请求应走 explicit_level
