"""Matrix 客户端链路 HTTP 会话复用回归测试（#39）。

核心断言：热路径不再每请求新建 aiohttp.ClientSession——
一次性函数走模块级惰性单例，常驻组件（文件桥 / 看门狗）自持一个会话并有关闭点。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import aiohttp

from src.matrix_client import http_session
from src.matrix_client.client_bootstrap import _homeserver_link_watchdog, fetch_matrix_agents
from src.matrix_client.file_bridge import MatrixFileBridge


class _FakeResp:
    def __init__(self, status: int = 200, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, responses: list[_FakeResp] | None = None):
        self._responses = list(responses or [])
        self.calls = 0
        self.closed = False

    def _next(self):
        self.calls += 1
        return self._responses.pop(0) if self._responses else _FakeResp(200)

    def post(self, url, **kwargs):
        return self._next()

    def get(self, url, **kwargs):
        return self._next()

    async def close(self):
        self.closed = True


async def test_shared_session_reused_within_loop_and_recreated_after_close():
    s1 = http_session.shared_matrix_http_session()
    assert http_session.shared_matrix_http_session() is s1
    await s1.close()
    s2 = http_session.shared_matrix_http_session()
    assert s2 is not s1
    await s2.close()


def test_shared_session_recreates_across_event_loops():
    async def grab():
        return http_session.shared_matrix_http_session()

    first = asyncio.run(grab())
    second = asyncio.run(grab())
    assert first is not second


async def test_file_bridge_lazy_session_reuse_and_aclose():
    bridge = MatrixFileBridge(homeserver="http://hs", client=SimpleNamespace(access_token="t"))
    s1 = bridge._http()
    assert bridge._http() is s1
    await bridge.aclose()
    assert s1.closed
    assert bridge._session is None
    s2 = bridge._http()
    assert s2 is not s1
    await bridge.aclose()


async def test_file_bridge_upload_uses_component_session(tmp_path):
    bridge = MatrixFileBridge(homeserver="http://hs", client=SimpleNamespace(access_token="t"))
    fake = _FakeSession([_FakeResp(500, {"err": "x"}), _FakeResp(200, {"content_uri": "mxc://y"})])
    bridge._session = fake
    target = tmp_path / "a.txt"
    target.write_text("hi")
    uri = await bridge._upload(target)
    assert uri == "mxc://y"
    # 第一个端点 500 后换第二个端点，全程同一个会话
    assert fake.calls == 2


async def test_watchdog_reuses_single_session_and_closes_on_cancel(monkeypatch):
    created = []

    class _Sess:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.closed = False
            self.probes = 0
            created.append(self)

        def get(self, url):
            self.probes += 1
            return _FakeResp(200)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(aiohttp, "ClientSession", _Sess)
    client = SimpleNamespace(homeserver="http://hs")
    task = asyncio.create_task(_homeserver_link_watchdog(client, label="t", interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(created) == 1
    assert created[0].probes >= 2
    assert created[0].closed


async def test_join_retry_shares_one_session_across_attempts(monkeypatch):
    from src.matrix_client import ingress_helpers

    fake = _FakeSession([_FakeResp(500, text="boom"), _FakeResp(200)])
    monkeypatch.setattr("src.matrix_client.http_session.shared_matrix_http_session", lambda: fake)
    ok, err = await ingress_helpers.matrix_join_room_with_retry(
        homeserver="http://hs", access_token="t", room_id="!r:x", base_delay=0
    )
    assert ok and err is None
    assert fake.calls == 2


async def test_check_homeserver_reachable_uses_shared_session(monkeypatch):
    from src.cli.matrix_connect import check_homeserver_reachable

    fake = _FakeSession([_FakeResp(200)])
    monkeypatch.setattr("src.matrix_client.http_session.shared_matrix_http_session", lambda: fake)
    assert await check_homeserver_reachable("http://hs/") is True
    assert fake.calls == 1


async def test_fetch_matrix_agents_uses_shared_session(monkeypatch):
    fake = _FakeSession([_FakeResp(200, {"agents": [{"name": "coara"}]})])
    monkeypatch.setattr("src.matrix_client.http_session.shared_matrix_http_session", lambda: fake)
    agents = await fetch_matrix_agents("http://hs")
    assert agents == [{"name": "coara"}]
    assert fake.calls == 1
