"""Tests for Web UI records list/get/delete/archive + user scope endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from src.records.agent_store import MemoryStore
from src.records.agent_types import MemoryEntry, MemorySource
from src.records.store import RecordsStore
from src.records.user_store import build_entry
from src.ui.web_server import WebServer


class _FakeRoot:
    def __init__(self, records_store=None, *, agent: MemoryStore | None = None) -> None:
        if records_store is not None:
            self.records_store = records_store
        elif agent is None:
            self.records_store = None
        else:
            self.records_store = type(
                "RS",
                (),
                {"agent": agent, "agent_root": agent.root, "user": None},
            )()


class _FakeRelUrl:
    def __init__(self, query: str = "") -> None:
        parsed = parse_qs(query, keep_blank_values=True)
        self.query = {k: v[0] if v else "" for k, v in parsed.items()}


class _FakeRequest:
    def __init__(self, query: str = "", body: dict | None = None) -> None:
        self.rel_url = _FakeRelUrl(query)
        self._body = body

    async def json(self) -> dict:
        if self._body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return self._body


def _make_server(tmp_path, monkeypatch, root: _FakeRoot) -> WebServer:
    server = WebServer.__new__(WebServer)
    server.workspace_dir = tmp_path
    server.coara_home = None
    server.root = root
    monkeypatch.setattr(server, "_check_token", lambda _request: None)
    return server


@pytest.mark.asyncio
async def test_memory_api_crud(tmp_path, monkeypatch) -> None:
    disabled = await _make_server(tmp_path, monkeypatch, _FakeRoot(None))._handle_records_list(_FakeRequest())
    assert json.loads(disabled.text) == {
        "enabled": False,
        "scope": "agent",
        "entries": [],
        "count": 0,
    }

    store = MemoryStore(tmp_path / "memory")
    now = datetime.now(UTC).astimezone()
    await store.write(
        MemoryEntry(
            id="mem_test_001",
            type="fact",
            title="测试事实",
            content="记录系统默认开启",
            created_at=now,
            source=MemorySource(session_id="s", actor="user"),
            tags=["test"],
        )
    )
    server = _make_server(tmp_path, monkeypatch, _FakeRoot(agent=store))

    listed = json.loads((await server._handle_records_list(_FakeRequest("query=默认"))).text)
    assert listed["enabled"] is True and listed["count"] == 1
    assert listed["scope"] == "agent"
    assert listed["entries"][0]["id"] == "mem_test_001"

    detail = json.loads((await server._handle_records_get(_FakeRequest("id=mem_test_001"))).text)
    assert detail["entry"]["title"] == "测试事实"

    await store.write(MemoryEntry(id="mem_test_002", type="decision", title="可删", content="临时", created_at=now))
    await server._handle_records_archive(_FakeRequest(body={"id": "mem_test_002"}))
    assert (await store.read("mem_test_002")).status == "archived"
    await server._handle_records_delete(_FakeRequest("id=mem_test_002"))
    assert await store.read("mem_test_002") is None


@pytest.mark.asyncio
async def test_user_scope_list_get_delete_and_file(tmp_path, monkeypatch) -> None:
    rs = RecordsStore(tmp_path / "records", agent_enabled=True)
    assert rs.user is not None
    entry = build_entry(title="说明.md", summary="文件收藏", source_type="file", content="")
    files_dir = rs.user.root / "files" / entry.id
    files_dir.mkdir(parents=True)
    (files_dir / "说明.md").write_text("# hello\n", encoding="utf-8")
    entry.content = f"文件：说明.md\n路径：`files/{entry.id}/说明.md`\n大小：9 字节"
    await rs.user.write(entry)

    server = _make_server(tmp_path, monkeypatch, _FakeRoot(rs))

    listed = json.loads((await server._handle_records_list(_FakeRequest("scope=user"))).text)
    assert listed["enabled"] is True and listed["scope"] == "user" and listed["count"] == 1
    assert listed["entries"][0]["id"] == entry.id
    assert listed["entries"][0]["has_file"] is True

    detail = json.loads(
        (await server._handle_records_get(_FakeRequest(f"scope=user&id={entry.id}"))).text
    )
    assert detail["entry"]["title"] == "说明.md"

    file_resp = await server._handle_records_file(_FakeRequest(f"id={entry.id}"))
    assert file_resp.status == 200
    assert Path(file_resp._path).name == "说明.md"  # type: ignore[attr-defined]

    await server._handle_records_delete(_FakeRequest(f"scope=user&id={entry.id}"))
    assert await rs.user.read(entry.id) is None
    assert not files_dir.exists()
