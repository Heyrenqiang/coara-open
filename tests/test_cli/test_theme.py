"""CLI 主题系统：语义令牌、暗/亮色切换、配置/环境变量解析。"""

from __future__ import annotations

import pytest

from src.cli import theme


@pytest.fixture(autouse=True)
def _reset_theme():
    yield
    theme.set_active_theme("dark")


def test_dark_theme_token_roles_complete() -> None:
    theme.set_active_theme("dark")
    t = theme.get_theme()
    for role in ("assistant", "assistant_label", "user", "tool", "tool_error", "muted"):
        value = getattr(t.text, role)
        assert isinstance(value, str) and value.startswith("#")
    for role in ("ok", "warn", "error", "info"):
        assert getattr(t.status, role).startswith("#")
    assert len(t.workspace_palette) >= 6
    assert all(bright != dim for bright, dim in t.workspace_palette)


def test_light_theme_token_roles_complete() -> None:
    theme.set_active_theme("light")
    t = theme.get_theme()
    assert t.text.assistant.startswith("#")
    assert t.status.error.startswith("#")
    # 亮主题 prompt chrome 也带 assistant 正文色
    assert t.prompt_style is not None


def test_fg_resolves_semantic_roles_and_follows_theme() -> None:
    theme.set_active_theme("dark")
    dark_assistant = theme.fg("text.assistant")
    theme.set_active_theme("light")
    light_assistant = theme.fg("text.assistant")
    assert dark_assistant != light_assistant
    assert theme.fg("status.error").startswith("#")


def test_fg_unknown_role_raises() -> None:
    with pytest.raises(KeyError):
        theme.fg("text.nonexistent")
    with pytest.raises(KeyError):
        theme.fg("bogus.role")


def test_resolve_theme_name() -> None:
    assert theme.resolve_theme_name("dark") == "dark"
    assert theme.resolve_theme_name(" LIGHT ") == "light"
    assert theme.resolve_theme_name("") is None
    assert theme.resolve_theme_name(None) is None
    assert theme.resolve_theme_name("solarized") is None


def test_apply_theme_from_config_precedence(monkeypatch) -> None:
    # 配置生效
    monkeypatch.delenv("COARA_CLI_THEME", raising=False)
    assert theme.apply_theme_from_config({"cli": {"theme": "light"}}) == "light"
    assert theme.get_theme().name == "light"
    # 环境变量覆盖配置
    monkeypatch.setenv("COARA_CLI_THEME", "dark")
    assert theme.apply_theme_from_config({"cli": {"theme": "light"}}) == "dark"
    assert theme.get_theme().name == "dark"
    # 缺省 dark；非法值回退 dark
    monkeypatch.delenv("COARA_CLI_THEME", raising=False)
    assert theme.apply_theme_from_config({}) == "dark"
    assert theme.apply_theme_from_config({"cli": {"theme": "neon"}}) == "dark"


def test_back_compat_getters_follow_active_theme() -> None:
    theme.set_active_theme("dark")
    assert theme.get_assistant_text_style() == theme.fg("text.assistant")
    assert theme.get_tool_text_style() == theme.fg("text.tool")
    assert theme.get_tool_error_style() == theme.fg("text.tool_error")
    assert theme.get_diff_colors() is theme.get_theme().diff
    assert theme.get_prompt_style() is theme.get_theme().prompt_style


def test_assistant_dimmer_than_user_and_brighter_than_tool() -> None:
    """视觉层次：用户输入最醒目，assistant 次之，工具行最弱（暗色主题）。"""

    def _lum(hex_color: str) -> float:
        r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    theme.set_active_theme("dark")
    t = theme.get_theme().text
    assert _lum(t.user) > _lum(t.assistant) > _lum(t.tool)
    # assistant 标签略亮于正文（角色标签可辨识）
    assert _lum(t.assistant_label) >= _lum(t.assistant)
