"""Web UI 打开/唤起决策：有活跃标签或刚有标签只唤起，确实没有才开新窗。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ui import web_tab_presence as presence


def _make_server(mod, *, has_active: bool, workspace_dir: Path):
    server = mod.WebServer.__new__(mod.WebServer)
    server.host = "127.0.0.1"
    server.port = 8080
    server.auth_token = "tok"
    server.workspace_dir = workspace_dir
    server.coara_home = None
    server.registry = SimpleNamespace(has_active=lambda: has_active)
    server._request_browser_focus = lambda path="": None  # type: ignore[method-assign]
    return server


async def test_decision_focuses_when_registry_has_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[str] = []
    raised: list[bool] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: raised.append(True))

    server = _make_server(mod, has_active=True, workspace_dir=tmp_path)
    server._request_browser_focus = lambda path="": focused.append(path)  # type: ignore[method-assign]

    result = await server.open_or_focus_decision()

    assert result["action"] == presence.ACTION_FOCUS_ACTIVE
    assert result["opened"] is False
    assert opened == []
    assert focused == [""]
    assert raised == [True]


async def test_decision_opens_when_no_tab_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)

    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)

    result = await server.open_or_focus_decision("/login")

    assert result["action"] == presence.ACTION_OPEN
    assert result["opened"] is True
    assert result["reason"] == "no-tab"
    assert opened == [server.build_url("/login")]


async def test_decision_focuses_recent_tab_without_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """刚有标签（含内核刚重启）：只唤起、绝不开新窗。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)
    monkeypatch.setattr(mod.web_tab_presence, "RECONNECT_GRACE_SECONDS", 0.0)

    presence.mark_tab_seen(tmp_path)
    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)
    server._request_browser_focus = lambda path="": focused.append(path)  # type: ignore[method-assign]

    result = await server.open_or_focus_decision()

    assert result["action"] == presence.ACTION_FOCUS_RECENT
    assert result["opened"] is False
    assert opened == []
    assert focused  # 至少推过一次 focus（唤醒）


async def test_decision_second_click_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """标签确实不在（关了浏览器但 TTL 内）时，再点一下就开新窗。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)
    monkeypatch.setattr(mod.web_tab_presence, "RECONNECT_GRACE_SECONDS", 0.0)

    presence.mark_tab_seen(tmp_path)
    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)

    first = await server.open_or_focus_decision()
    second = await server.open_or_focus_decision()

    assert first["opened"] is False
    assert second["opened"] is True
    assert second["reason"] == "second-click"
    assert len(opened) == 1


async def test_focus_endpoint_never_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/api/ui/focus 兼容入口：只唤起，永不开窗。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)

    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)
    server._check_token = lambda request: None  # type: ignore[method-assign]

    request = SimpleNamespace(query={})
    response = await server._handle_ui_focus(request)

    assert opened == []
    assert b'"focused": false' in response.body


def test_open_window_without_loop_falls_back_to_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """托盘线程无 loop 可用时（异常态）直接开窗，绝不让「点了没反应」。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)

    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)

    url = server.open_window()

    assert url == "http://127.0.0.1:8080?token=tok"
    assert opened == [url]


def test_open_or_focus_uses_active_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.ui import web_server as mod

    calls: list[str] = []

    class _Srv:
        def open_window(self, path: str = "") -> str:
            calls.append(path)
            return "http://x"

    monkeypatch.setattr(mod, "_ACTIVE_WEB_SERVER", _Srv())
    assert mod.open_or_focus_web_ui(path="/me") == "http://x"
    assert calls == ["/me"]


def test_open_or_focus_requests_server_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """外进程不再自己 webbrowser.open，而是请内核做判定。"""
    from src.ui import web_server as mod

    asked: list[dict[str, str]] = []
    opened: list[str] = []
    monkeypatch.setattr(mod, "_ACTIVE_WEB_SERVER", None)
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)
    monkeypatch.setattr(
        mod,
        "_request_server_open",
        lambda **kwargs: asked.append(kwargs) or True,
    )

    url = mod.open_or_focus_web_ui(path="/chat", host="127.0.0.1", port=8080, token="tok")

    assert asked and asked[0]["path"] == "/chat"
    assert opened == []
    assert url == "http://127.0.0.1:8080/chat?token=tok"
