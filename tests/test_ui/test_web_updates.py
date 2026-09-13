"""批复中心（工作空间动态收件箱）Web 接口测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from src.ui.web_server import WebServer
from src.workspace.updates.store import WorkspaceUpdatesStore


class _FakeRelUrl:
    def __init__(self, query: str = "") -> None:
        parsed = parse_qs(query, keep_blank_values=True)
        self.query = {k: v[0] if v else "" for k, v in parsed.items()}


class _FakeRequest:
    def __init__(self, query: str = "", body: dict | None = None) -> None:
        self.rel_url = _FakeRelUrl(query)
        self.query = self.rel_url.query
        self._body = body

    async def json(self) -> dict:
        return self._body or {}


def _seed(store: WorkspaceUpdatesStore, **overrides):
    params = {
        "workspace": "shop",
        "source_id": "test",
        "event_type": "reminder_due",
        "dedupe_key": f"k-{overrides.get('salience', 'normal')}-{overrides.get('workspace', '')}",
        "text": "正文",
        "payload": {"title": "标题"},
        "type": "reminder",
    }
    params.update(overrides)
    return store.append(**params)


class _FakeCoara:
    def __init__(self, *, busy: bool = False) -> None:
        self._process_lock = asyncio.Lock()
        self._busy = busy
        self.received: list[str] = []

    def has_active_turn(self) -> bool:
        return self._busy

    def submit_continuation_input(self, text: str) -> None:
        self.received.append(text)

    async def process_message(self, content: str, **kwargs):
        self.received.append(content)
        if False:
            yield ""  # async generator protocol

    def persist_session_to_disk(self) -> None:
        pass


class _FakeRoot:
    def __init__(self, store: WorkspaceUpdatesStore, coara: _FakeCoara | None = None) -> None:
        self._store = store
        entry = SimpleNamespace(id="ws-1", name="shop")
        self.workspace_manager = SimpleNamespace(
            registry=SimpleNamespace(resolve_name_or_id=lambda _name: entry),
        )
        self._entry = entry
        self._coara = coara or _FakeCoara()

    def _updates_store(self):
        return self._store

    async def ensure_workspace_session(self, entry):
        assert entry is self._entry
        return SimpleNamespace(coara=self._coara)


def _make_server(tmp_path, monkeypatch, root) -> WebServer:
    server = WebServer.__new__(WebServer)
    server.workspace_dir = tmp_path
    server.coara_home = None
    server.root = root
    monkeypatch.setattr(server, "_check_token", lambda _request: None)
    return server


@pytest.mark.asyncio
async def test_updates_list_summary_pending(tmp_path, monkeypatch) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    _seed(store, dedupe_key="a", salience="high")
    _seed(store, dedupe_key="b", salience="normal")
    _seed(store, dedupe_key="c", workspace="other", salience="high")
    root = _FakeRoot(store)
    server = _make_server(tmp_path, monkeypatch, root)

    resp = await server._handle_updates_summary(_FakeRequest())
    data = json.loads(resp.text)
    assert data["unread"] == {"shop": 2, "other": 1}
    assert data["pending"] == 2

    resp = await server._handle_updates_list(_FakeRequest("workspace=shop&status=unread"))
    items = json.loads(resp.text)["items"]
    assert len(items) == 2
    assert {i["salience"] for i in items} == {"high", "normal"}

    resp = await server._handle_updates_list(_FakeRequest("salience=high"))
    items = json.loads(resp.text)["items"]
    assert len(items) == 2

    resp = await server._handle_updates_pending(_FakeRequest())
    items = json.loads(resp.text)["items"]
    assert len(items) == 2
    assert all(i["salience"] == "high" for i in items)


@pytest.mark.asyncio
async def test_updates_read_archive_mark_read(tmp_path, monkeypatch) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    m1 = _seed(store, dedupe_key="a")
    m2 = _seed(store, dedupe_key="b")
    server = _make_server(tmp_path, monkeypatch, _FakeRoot(store))

    resp = await server._handle_updates_read(_FakeRequest(body={"message_id": m1.message_id}))
    assert json.loads(resp.text)["ok"] is True
    assert store.get(m1.message_id).status == "read"

    resp = await server._handle_updates_archive(_FakeRequest(body={"message_id": m2.message_id}))
    assert json.loads(resp.text)["ok"] is True
    assert store.get(m2.message_id).status == "archived"

    resp = await server._handle_updates_read(_FakeRequest(body={"message_id": "upd-none"}))
    assert resp.status == 404

    m3 = _seed(store, dedupe_key="c")
    resp = await server._handle_updates_mark_read(_FakeRequest(body={"workspace": "shop"}))
    assert json.loads(resp.text)["ok"] is True
    assert store.unread_count("shop") == 0
    assert m3 is not None


@pytest.mark.asyncio
async def test_updates_review_routes_to_owning_workspace(tmp_path, monkeypatch) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _seed(store, dedupe_key="a", salience="high")
    coara = _FakeCoara()
    server = _make_server(tmp_path, monkeypatch, _FakeRoot(store, coara))

    resp = await server._handle_updates_review(
        _FakeRequest(body={"message_id": msg.message_id, "text": "处理一下这个"})
    )
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["delivered"] == "started"
    assert data["workspace"] == "shop"

    # 后台回合异步执行，让事件循环跑一轮
    for _ in range(50):
        if coara.received:
            break
        await asyncio.sleep(0.02)
    assert len(coara.received) == 1
    content = coara.received[0]
    assert '<工作空间消息 workspace="shop"' in content
    assert msg.message_id in content
    assert "处理一下这个" in content
    # 批复后条目自动标已读
    assert store.get(msg.message_id).status == "read"


@pytest.mark.asyncio
async def test_updates_review_busy_session_queues(tmp_path, monkeypatch) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _seed(store, dedupe_key="a")
    coara = _FakeCoara(busy=True)
    server = _make_server(tmp_path, monkeypatch, _FakeRoot(store, coara))

    resp = await server._handle_updates_review(_FakeRequest(body={"message_id": msg.message_id, "text": "稍后处理"}))
    data = json.loads(resp.text)
    assert data["delivered"] == "queued"
    assert len(coara.received) == 1
    assert "稍后处理" in coara.received[0]


@pytest.mark.asyncio
async def test_updates_review_missing_message(tmp_path, monkeypatch) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    server = _make_server(tmp_path, monkeypatch, _FakeRoot(store))
    resp = await server._handle_updates_review(_FakeRequest(body={"message_id": "upd-none", "text": "x"}))
    assert resp.status == 404
