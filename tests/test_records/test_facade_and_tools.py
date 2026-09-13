"""Tests for RecordsFacade, RecordsStore, and tools."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.types import RecordsConfig
from src.records.facade import RecordsFacade
from src.records.store import RecordsStore
from src.tools.builtin.records.local_search import LocalSearchTool
from src.tools.builtin.records.record import RecordTool


@pytest.fixture
def store(tmp_path):
    return RecordsStore(tmp_path / "records", agent_enabled=True)


@pytest.fixture
def facade(store):
    return RecordsFacade(store)


@pytest.mark.asyncio
async def test_facade_joint_search(facade):
    await facade.add_agent(
        content="工作流容器拖拽应带动子节点",
        title="容器焊接",
        type_="decision",
        tags=["workflow"],
        session_id="sess",
    )
    await facade.add_user(
        title="Agent Memory 综述",
        summary="- 全景\n- 对比",
        url="https://example.com/am",
        tags=["agent"],
        content="原文",
    )

    result = await facade.search(query="agent", origin="all", limit=5)
    assert not result.is_error
    assert "【收藏 · user】" in result.message
    assert "Agent Memory 综述" in result.message

    result = await facade.search(query="容器", origin="agent", limit=5)
    assert "【记录 · agent】" in result.message
    assert "容器焊接" in result.message


@pytest.mark.asyncio
async def test_facade_search_touch_batched(facade, monkeypatch):
    """search 命中 touch 批量化：多条命中只合并写一次 agent 侧 index.yaml。"""
    for i in range(3):
        added = await facade.add_agent(
            content=f"批量化目标内容 {i}",
            title=f"批量 {i}",
            type_="event",
            session_id="sess",
        )
        assert not added.is_error

    saves = 0
    orig_save = facade.store.agent._save_index

    def counting_save(index):
        nonlocal saves
        saves += 1
        orig_save(index)

    monkeypatch.setattr(facade.store.agent, "_save_index", counting_save)
    result = await facade.search(query="批量化目标", origin="agent", limit=5)
    assert not result.is_error
    assert result.metadata["count"] == 3
    assert saves == 1


@pytest.mark.asyncio
async def test_record_tool_add_agent_and_remove(facade):
    parent = SimpleNamespace(session_id="sess-tool")
    tool = RecordTool(facade=facade, parent_coara=parent)  # type: ignore[arg-type]

    result = await tool.create_invocation(
        {
            "action": "add",
            "content": "工作流容器拖拽应带动子节点",
            "type": "decision",
            "title": "容器焊接",
            "tags": ["workflow"],
        }
    ).execute()
    assert not result.is_error
    mid = result.metadata.get("id")
    assert mid

    search = LocalSearchTool(facade=facade)
    recall = await search.create_invocation(
        {"action": "search", "query": "容器", "origin": "agent", "limit": 3}
    ).execute()
    assert not recall.is_error
    assert mid in (recall.metadata or {}).get("ids", [])

    forgot = await tool.create_invocation({"action": "remove", "id": mid}).execute()
    assert not forgot.is_error
    assert await facade.store.agent.read(mid) is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_record_tool_update(facade):
    tool = RecordTool(facade=facade, parent_coara=SimpleNamespace(session_id="s"))  # type: ignore[arg-type]
    added = await tool.create_invocation(
        {"action": "add", "content": "初版内容", "title": "容器焊接", "type": "decision"}
    ).execute()
    assert not added.is_error
    mid = added.metadata.get("id")

    updated = await tool.create_invocation({"action": "update", "id": mid, "content": "改写后的完整内容"}).execute()
    assert not updated.is_error

    entry = await facade.store.agent.read(mid)  # type: ignore[union-attr]
    assert entry is not None
    assert entry.content == "改写后的完整内容"
    assert entry.title == "容器焊接"

    search = LocalSearchTool(facade=facade)
    recall = await search.create_invocation(
        {"action": "search", "query": "改写后", "origin": "agent", "limit": 3}
    ).execute()
    assert mid in (recall.metadata or {}).get("ids", [])

    missing = await tool.create_invocation({"action": "update", "id": "nope", "content": "x"}).execute()
    assert missing.is_error
    empty = await tool.create_invocation({"action": "update", "id": mid, "content": "  "}).execute()
    assert empty.is_error


@pytest.mark.asyncio
async def test_record_tool_rejects_secret(facade):
    tool = RecordTool(facade=facade, parent_coara=SimpleNamespace(session_id="s"))  # type: ignore[arg-type]
    result = await tool.create_invocation({"action": "add", "content": "password = supersecret"}).execute()
    assert result.is_error
    assert "敏感" in str(result.content)


@pytest.mark.asyncio
async def test_record_tool_without_store():
    tool = RecordTool(store=None)
    result = await tool.create_invocation({"action": "add", "content": "x"}).execute()
    assert result.is_error
    assert "未启用" in str(result.content)


@pytest.mark.asyncio
async def test_record_rejects_user_origin(facade):
    tool = RecordTool(facade=facade)
    result = await tool.create_invocation(
        {
            "action": "add",
            "origin": "user",
            "title": "Agent Memory 综述",
            "summary": "- 全景\n- 对比",
            "url": "https://example.com/am",
            "content": "原文",
        }
    ).execute()
    assert result.is_error
    assert "手点" in str(result.content) or "不能用 record" in str(result.content)

    await facade.add_user(
        title="Agent Memory 综述",
        summary="- 全景\n- 对比",
        url="https://example.com/am",
        content="原文",
    )
    search = LocalSearchTool(facade=facade)
    found = await search.create_invocation({"action": "search", "query": "memory", "origin": "user"}).execute()
    assert not found.is_error
    assert "Agent Memory 综述" in found.content


@pytest.mark.asyncio
async def test_record_rejects_unknown_origin(facade):
    tool = RecordTool(facade=facade)
    result = await tool.create_invocation(
        {"action": "add", "origin": "somewhere", "content": "笔记"}
    ).execute()
    assert result.is_error
    assert "origin=" in str(result.content) or "仅写 agent" in str(result.content)


@pytest.mark.asyncio
async def test_local_search_source_replays_tape(facade):
    parent = SimpleNamespace(
        session_id="sess-src",
        workspace_dir="",
        identity=SimpleNamespace(persona=SimpleNamespace(name="daily")),
    )
    tool = RecordTool(facade=facade, parent_coara=parent)  # type: ignore[arg-type]
    added = await tool.create_invocation(
        {"action": "add", "content": "可回放笔记正文", "title": "回放测"}
    ).execute()
    assert not added.is_error
    mid = added.metadata.get("id")
    assert mid

    # source on record must redirect
    bad = await tool.create_invocation({"action": "source", "id": mid}).execute()
    assert bad.is_error
    assert "local_search" in str(bad.content)

    search = LocalSearchTool(facade=facade)
    # No tape coords → still a structured reply, not crash
    src = await search.create_invocation({"action": "source", "id": mid}).execute()
    assert src is not None


@pytest.mark.asyncio
async def test_record_digest_write(facade):
    tool = RecordTool(facade=facade, parent_coara=SimpleNamespace(session_id="s"))  # type: ignore[arg-type]
    result = await tool.create_invocation(
        {
            "action": "digest_write",
            "date": "2026-08-04",
            "content": "# 日报\n\n完成了双任务接线。",
        }
    ).execute()
    assert not result.is_error
    path = facade.store.agent.digest_path("2026-08-04")  # type: ignore[union-attr]
    assert path.is_file()
    assert "双任务" in path.read_text(encoding="utf-8")


def test_records_config_defaults_on():
    cfg = RecordsConfig()
    assert cfg.enabled is True
    assert cfg.daily_curator_enabled is True


@pytest.mark.asyncio
async def test_add_agent_same_content_dedupes_to_single_entry(facade):
    """去重 hash 与落盘 body 同一口径：同内容 add 两次只留一条（第二次 skip + touch）。"""
    assert facade.store is not None and facade.store.agent is not None
    first = await facade.add_agent(content="项目采用 asyncio 单进程多协程", title="技术选型", session_id="s-dedupe")
    assert first.ok, first.message
    assert first.metadata is not None and first.metadata.get("deduped") is not True

    second = await facade.add_agent(content="项目采用 asyncio 单进程多协程", title="技术选型", session_id="s-dedupe")
    assert second.ok, second.message
    assert second.metadata is not None
    assert second.metadata.get("deduped") is True
    assert second.metadata.get("id") == first.metadata.get("id")

    # 落盘只有一个内容条目
    entries = await facade.store.agent.search(query="asyncio", limit=10)
    assert len([e for e in entries if e.status == "active"]) == 1


def test_local_search_deferred_by_default():
    assert LocalSearchTool.should_defer is True
    assert RecordTool.should_defer is False


@pytest.mark.asyncio
async def test_write_digest_mirrors_into_agent_records(facade, store):
    """日报写入后镜像进记录流：RecordsView 可见、local_search 可检索；同日同内容重跑去重。"""
    result = await facade.write_digest("2026-08-17", "# 日报 · 2026-08-17\n\n昨天完成了工作流独立化")
    assert not result.is_error
    entries = await store.agent.list_entries(tag="daily")
    assert len(entries) == 1
    assert "日报 · 2026-08-17" in entries[0].title
    assert "工作流独立化" in entries[0].content
    # 同日同内容重跑：内容级去重，不产生第二条
    again = await facade.write_digest("2026-08-17", "# 日报 · 2026-08-17\n\n昨天完成了工作流独立化")
    assert not again.is_error
    entries2 = await store.agent.list_entries(tag="daily")
    assert len(entries2) == 1
