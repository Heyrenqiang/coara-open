"""Web UI 打开/唤起决策：有活跃标签先唤起；presence 未过期先等重连；仍没有则开新标签。"""

from __future__ import annotations

import asyncio
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
    server._bg_tasks = set()
    return server


async def _drain_bg(server) -> None:
    """等决策里 fire-and-forget 的开窗/抬窗任务跑完（单测断言 opened 列表用）。"""
    bg = getattr(server, "_bg_tasks", None)
    while bg:
        await asyncio.gather(*list(bg), return_exceptions=True)


async def test_decision_focuses_when_registry_has_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[str] = []
    raised: list[bool] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: raised.append(True) or True)

    server = _make_server(mod, has_active=True, workspace_dir=tmp_path)
    server._request_browser_focus = lambda path="": focused.append(path)  # type: ignore[method-assign]

    result = await server.open_or_focus_decision()
    await _drain_bg(server)

    assert result["action"] == presence.ACTION_FOCUS_ACTIVE
    assert result["reason"] == "active"
    assert result["opened"] is False
    assert opened == []
    assert focused == [""]
    assert raised == [True]


async def test_decision_opens_when_active_but_raise_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已有 WS，但窗口标题不是 coara（标签在后台）→ 开入口 URL 把用户带到 coara。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: False)

    server = _make_server(mod, has_active=True, workspace_dir=tmp_path)
    server._request_browser_focus = lambda path="": focused.append(path)  # type: ignore[method-assign]

    result = await server.open_or_focus_decision("/chat")
    await _drain_bg(server)

    assert result["action"] == presence.ACTION_FOCUS_ACTIVE
    assert result["reason"] == "active-background-nudge"
    assert result["opened"] is True
    assert focused == ["/chat"]
    assert len(opened) == 1
    assert "/chat" in opened[0]
    assert "token=tok" in opened[0]


async def test_decision_opens_when_no_tab_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)

    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)

    result = await server.open_or_focus_decision("/login")
    await _drain_bg(server)

    assert result["action"] == presence.ACTION_OPEN
    assert result["opened"] is True
    assert result["reason"] == "no-tab"
    assert len(opened) == 1
    assert "token=tok" in opened[0]
    assert "/login?" in opened[0] or opened[0].endswith("/login?token=tok") or "/login?token=tok" in opened[0]
    assert "_open=" in opened[0]


async def test_decision_recent_tab_reconnected_does_not_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """刚有标签且重连窗内 WS 恢复：抬到前台成功则只唤起，不开窗。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: True)

    presence.mark_tab_seen(tmp_path)
    # 重连窗内翻成活跃：模拟旧标签自动重连成功
    active = {"v": False}
    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)
    server.registry = SimpleNamespace(has_active=lambda: active["v"])
    server._request_browser_focus = lambda path="": focused.append(path)  # type: ignore[method-assign]

    async def _wait(_seconds: float) -> bool:
        active["v"] = True
        return True

    server._wait_for_tab_reconnect = _wait  # type: ignore[method-assign]

    result = await server.open_or_focus_decision()
    await _drain_bg(server)

    assert result["action"] == presence.ACTION_FOCUS_RECENT
    assert result["reason"] == "recent-reconnected"
    assert result["opened"] is False
    assert opened == []
    assert focused  # 至少推过 focus


async def test_decision_recent_tab_reconnected_nudge_when_raise_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """重连成功但抬窗失败（后台标签）→ 开入口 URL。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: False)

    presence.mark_tab_seen(tmp_path)
    active = {"v": False}
    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)
    server.registry = SimpleNamespace(has_active=lambda: active["v"])

    async def _wait(_seconds: float) -> bool:
        active["v"] = True
        return True

    server._wait_for_tab_reconnect = _wait  # type: ignore[method-assign]

    result = await server.open_or_focus_decision()
    await _drain_bg(server)

    assert result["action"] == presence.ACTION_FOCUS_RECENT
    assert result["reason"] == "recent-background-nudge"
    assert result["opened"] is True
    assert len(opened) == 1


async def test_decision_recent_tab_timeout_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """刚有 presence 但重连超时（浏览器已关 / 标签已死）：开新窗，避免点托盘没反应。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)
    monkeypatch.setattr(mod.web_tab_presence, "RECONNECT_PROBE_SECONDS", 0.0)

    presence.mark_tab_seen(tmp_path)
    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)
    server._request_browser_focus = lambda path="": focused.append(path)  # type: ignore[method-assign]

    result = await server.open_or_focus_decision("/config")
    await _drain_bg(server)

    assert result["action"] == presence.ACTION_OPEN
    assert result["reason"] == "recent-gone"
    assert result["opened"] is True
    assert len(opened) == 1
    assert focused  # 仍先尝试过唤起
    assert "/config" in opened[0]


async def test_decision_recent_always_uses_short_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """即便抬到了含 coara 标题的窗，重连也只短探——满额 grace 会让托盘空等。"""
    from src.ui import web_server as mod

    waited: list[float] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: None)
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: True)
    monkeypatch.setattr(mod.web_tab_presence, "RECONNECT_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(mod.web_tab_presence, "RECONNECT_PROBE_SECONDS", 0.15)

    presence.mark_tab_seen(tmp_path)
    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)

    async def _wait(seconds: float) -> bool:
        waited.append(seconds)
        return False

    server._wait_for_tab_reconnect = _wait  # type: ignore[method-assign]

    result = await server.open_or_focus_decision()
    await _drain_bg(server)

    assert result["opened"] is True
    assert waited == [0.15]


async def test_decision_repeated_focus_when_active_never_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已有活跃 WS 且能抬到前台时：反复点打开只唤起，绝不新开。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: True)

    presence.mark_tab_seen(tmp_path)
    server = _make_server(mod, has_active=True, workspace_dir=tmp_path)

    first = await server.open_or_focus_decision()
    second = await server.open_or_focus_decision()
    await _drain_bg(server)

    assert first["opened"] is False
    assert second["opened"] is False
    assert first["action"] == presence.ACTION_FOCUS_ACTIVE
    assert opened == []


async def test_focus_endpoint_never_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/api/ui/focus：只唤起，无标签也不开新窗（开窗由托盘本机决定）。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)

    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)
    server._check_token = lambda request: None  # type: ignore[method-assign]

    request = SimpleNamespace(query={})
    response = await server._handle_ui_focus(request)
    await _drain_bg(server)

    assert opened == []
    assert b'"opened": false' in response.body
    assert b"focus-only" in response.body


async def test_decision_allow_open_false_skips_nudge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """allow_open=False：有活跃 WS 但抬窗失败也不 webbrowser.open。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: False)

    server = _make_server(mod, has_active=True, workspace_dir=tmp_path)
    result = await server.open_or_focus_decision(allow_open=False)
    await _drain_bg(server)

    assert result["opened"] is False
    assert result["reason"] == "active-focus-only"
    assert opened == []


def test_open_window_without_loop_falls_back_to_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """托盘线程无 loop 可用时（异常态）直接开窗，绝不让「点了没反应」。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: None)

    server = _make_server(mod, has_active=False, workspace_dir=tmp_path)

    url = server.open_window("/x")
    assert "token=tok" in url
    assert len(opened) == 1


def test_open_or_focus_prefers_local_raise_then_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外进程：先本地抬窗；抬不到再开入口。内核只走 focus，不开第二页。"""
    from src.ui import web_server as mod

    opened: list[str] = []
    focused: list[dict[str, str]] = []
    monkeypatch.setattr(mod, "_ACTIVE_WEB_SERVER", None)
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: False)
    monkeypatch.setattr(
        mod,
        "_request_server_focus",
        lambda **kwargs: focused.append(kwargs) or {"opened": False},
    )

    url = mod.open_or_focus_web_ui(path="/chat", host="127.0.0.1", port=8080, token="tok")

    assert url == "http://127.0.0.1:8080/chat?token=tok"
    assert opened == [url]


def test_open_or_focus_skips_open_when_local_raise_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ui import web_server as mod

    opened: list[str] = []
    monkeypatch.setattr(mod, "_ACTIVE_WEB_SERVER", None)
    monkeypatch.setattr(mod, "_open_web_ui_window", lambda url: opened.append(url))
    monkeypatch.setattr(mod, "_try_raise_coara_browser_windows", lambda: True)
    monkeypatch.setattr(mod, "_request_server_focus", lambda **_: {"opened": False})
    monkeypatch.setattr(mod, "_request_server_open", lambda **_: (_ for _ in ()).throw(AssertionError("must not open")))

    url = mod.open_or_focus_web_ui(path="/", host="127.0.0.1", port=8080, token="tok")

    assert url == "http://127.0.0.1:8080/?token=tok"
    assert opened == []


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
