"""Web 启动横幅的 provider/model 显示：前台会话（含空间级 LLM 绑定）优先。"""

from __future__ import annotations

from src.ui.web_server import _resolve_banner_provider_model


class _FakeForeground:
    def __init__(self, provider: str = "", model: str = ""):
        self.provider_name = provider
        self.model_name = model


class _FakeRoot:
    def __init__(self, fg: object | None = None, *, raise_on_fg: bool = False):
        self._fg = fg
        self._raise_on_fg = raise_on_fg

    @property
    def foreground_coara(self) -> object:
        if self._raise_on_fg:
            raise RuntimeError("Foreground WorkspaceSession is not bound")
        if self._fg is None:
            raise RuntimeError("Foreground WorkspaceSession is missing")
        return self._fg


def test_foreground_binding_wins_over_passed_args():
    root = _FakeRoot(_FakeForeground(provider="deepseek", model="deepseek-flash"))
    provider, model = _resolve_banner_provider_model(root, "minimax", "MiniMax-M3")
    assert provider == "deepseek"
    assert model == "deepseek-flash"


def test_passed_args_used_when_foreground_has_no_binding():
    root = _FakeRoot(_FakeForeground(provider="", model=""))
    provider, model = _resolve_banner_provider_model(root, "minimax", "MiniMax-M3")
    assert provider == "minimax"
    assert model == "MiniMax-M3"


def test_passed_args_used_when_foreground_unbound():
    root = _FakeRoot(_FakeForeground(provider="", model=""))
    provider, model = _resolve_banner_provider_model(root, "kimi", "k3")
    assert provider == "kimi"
    assert model == "k3"


def test_fallback_when_foreground_session_missing():
    root = _FakeRoot(raise_on_fg=True)
    provider, model = _resolve_banner_provider_model(root, "agnes", "agnes-2.5-flash")
    assert provider == "agnes"
    assert model == "agnes-2.5-flash"


def test_empty_args_with_no_binding():
    root = _FakeRoot(_FakeForeground(provider="", model=""))
    provider, model = _resolve_banner_provider_model(root, None, None)
    assert provider == ""
    assert model == ""
