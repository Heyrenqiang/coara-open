"""build_url 带查询串路径的拼接回归测试。

回归场景（09-19 真机截图）：新装机无 key 时托盘把首页路径定为
/config?focus=models，build_url 无条件接 ?token= 产生第二个问号
（/config?focus=models?token=xxx），token 不成参数、前端全部 API 401，
页面内容全空。路由已含 ? 时必须用 & 续接。
"""

from __future__ import annotations

from src.ui import web_server
from src.ui.web_link import build_web_url


def _make_server() -> web_server.WebServer:
    server = web_server.WebServer.__new__(web_server.WebServer)
    server.host = "127.0.0.1"
    server.port = 8080
    server.auth_token = "tok123"
    return server


def test_build_url_plain_path_uses_question_mark() -> None:
    server = _make_server()
    assert server.build_url("/") == "http://127.0.0.1:8080/?token=tok123"


def test_build_url_path_with_query_uses_ampersand() -> None:
    server = _make_server()
    url = server.build_url("/config?focus=models")
    assert url == "http://127.0.0.1:8080/config?focus=models&token=tok123"


def test_build_url_bust_appends_after_token() -> None:
    server = _make_server()
    url = server.build_url("/config?focus=models", bust=True)
    assert url.startswith("http://127.0.0.1:8080/config?focus=models&token=tok123&_open=")


def test_open_or_focus_entry_query_path(monkeypatch, tmp_path) -> None:
    # open_or_focus_web_ui 的本地回退分支与 build_url 同口径
    captured: dict[str, str] = {}

    monkeypatch.setattr(web_server, "_ACTIVE_WEB_SERVER", None)
    monkeypatch.setattr(web_server, "_request_server_open", lambda **_: False)
    monkeypatch.setattr(web_server, "_open_web_ui_window", lambda url: captured.setdefault("url", url))
    monkeypatch.setattr(web_server, "_try_raise_coara_browser_windows", lambda: None)

    result = web_server.open_or_focus_web_ui(host="127.0.0.1", port=8080, token="t", path="/config?focus=models")
    assert result == "http://127.0.0.1:8080/config?focus=models&token=t"
    assert captured["url"] == result


def test_build_web_url_query_path(monkeypatch, tmp_path) -> None:
    from src.ui import web_link

    monkeypatch.setattr(web_link, "load_web_token", lambda _ws: "tok")
    monkeypatch.setattr(web_link, "resolve_web_host_port", lambda: ("127.0.0.1", 8080))
    url = build_web_url(tmp_path, "/config?focus=models")
    assert url == "http://127.0.0.1:8080/config?focus=models&token=tok"
